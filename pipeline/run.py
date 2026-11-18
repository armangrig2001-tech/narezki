#!/usr/bin/env python3
"""Конвейер: поиск клипов → очередь → подробная оценка → дневная подборка → монтаж.

Запуск (GitHub Actions, workflow «Clip Pipeline»):
  python -m pipeline.run --phase plan   # поиск, история просмотров, нужна ли работа (stdlib)
  python -m pipeline.run --phase work   # загрузка, Whisper, Claude/метаданные, решения, рендер

Параметры: --clip-url (проверка конкретного клипа), --dry-run (отчёт без рендера и без записи
состояния), --force (смонтировать повторно), --include-clip (тест: добавить клип к поиску).
"""
import argparse
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone

from . import config as C
from . import select as S
from . import state as ST


# ---------- отчёт ----------

class Report:
    def __init__(self):
        self.lines, self.rows, self.notes = [], [], []
        self.data = {"excluded_processed": [], "duplicates": [], "evaluated": [], "render_queue": [],
                     "rendered": [], "failed": [], "skipped": [], "errors": []}

    def row(self, rec, status, extra=""):
        w = rec.get("window")
        tc = f"{w[0]:.1f}–{w[1]:.1f} с" if w else "—"
        score = rec.get("score")
        mode = {"claude": "Claude", "meta": "метаданные"}.get(rec.get("score_mode"), "")
        sc = f"{score} ({mode})" if score is not None else "—"
        reason = (rec.get("reason") or "").replace("|", "/").replace("\n", " ")
        self.rows.append(f"| [{rec['id'][:24]}]({rec['url']}) | {sc} | {reason[:160] or '—'} | {tc} | "
                         f"{status}{(' — ' + extra) if extra else ''} |")

    def write(self, path_md, path_json, header):
        md = [header, ""]
        if self.rows:
            md += ["| Клип | Балл | Причина выбора | Таймкод монтажа | Статус |", "|---|---|---|---|---|"]
            md += self.rows + [""]
        md += self.lines
        if self.notes:
            md += ["", "**Заметки:**"] + [f"- {n}" for n in self.notes]
        text = "\n".join(md) + "\n"
        with open(path_md, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(path_json, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        gs = os.environ.get("GITHUB_STEP_SUMMARY")
        if gs:
            with open(gs, "a", encoding="utf-8") as fh:
                fh.write(text)
        print(text)


def gh_output(**kw):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a") as fh:
            for k, v in kw.items():
                fh.write(f"{k}={v}\n")


# ---------- записи клипов ----------

def upsert(st, clip, now, game_id=None):
    cid = clip["id"]
    rec = st["clips"].setdefault(cid, {"id": cid, "status": "new", "attempts": 0,
                                       "first_seen": S.iso(now)})
    rec.update({
        "url": clip.get("url") or f"https://clips.twitch.tv/{cid}",
        "created_at": clip["created_at"], "title": clip.get("title", ""),
        "video_id": clip.get("video_id") or None, "vod_offset": clip.get("vod_offset"),
        "duration": float(clip.get("duration") or 0), "clip_duration": float(clip.get("duration") or 0),
        "game_id": clip.get("game_id"),
        "slug": cid, "channel": C.CHANNEL, "creator": clip.get("creator_name") or clip.get("creator_id"),
    })
    if game_id and clip.get("game_id") != game_id:
        rec["other_game"] = True
    S.record_views(rec, clip.get("view_count", 0), now)
    return rec


def window_clips(st, now, hours=72):
    lim = now - timedelta(hours=hours)
    return {cid: r for cid, r in st["clips"].items()
            if S.parse_ts(r["created_at"]) >= lim and not r.get("other_game")}


# ---------- фаза plan ----------

def phase_plan(args, st, rep, now):
    from .twitch import Twitch, clip_id_from_url
    tw = Twitch()
    s = tw.stream(C.CHANNEL)
    live = bool(s)
    stream = st["stream"]
    stream.update({"live": live, "checked_at": S.iso(now)})
    if live:
        stream["last_live_at"] = S.iso(now)
        stream["started_at"] = s.get("started_at")
    bid = tw.user_id(C.CHANNEL)
    all_cats = (C.CATEGORY or "").lower() == "all"
    gid = None if all_cats else tw.game_id(C.CATEGORY)
    if not all_cats and not gid:
        raise RuntimeError(f"категория «{C.CATEGORY}» не найдена в Twitch")
    st["game_id"] = gid
    found = 0
    if args.clip_url:
        cid = clip_id_from_url(args.clip_url) or args.clip_url.strip()
        clip = tw.clip(cid)
        if not clip:
            raise RuntimeError(f"клип {cid} не найден в Twitch Helix")
        rec = upsert(st, clip, now, gid)
        st["manual"] = {"id": rec["id"], "force": bool(args.force)}
        if rec.get("other_game"):
            rep.notes.append(f"клип {cid} не из категории {C.CATEGORY} — проверка всё равно выполнена")
            rec.pop("other_game", None)
        found = 1
    else:
        clips = tw.clips(bid, C.DISCOVERY_HOURS)
        excluded_games = set()
        if all_cats and C.PROFILE.get("exclude_games"):
            ids = sorted({c.get("game_id") for c in clips if c.get("game_id")})
            names = {}
            for i in range(0, len(ids), 100):
                for g in tw.get("games", {"id": ids[i:i + 100]}).get("data", []):
                    names[g["id"]] = g["name"]
            excluded_games = {i for i, n in names.items() if n in C.PROFILE["exclude_games"]}
        for c in clips:
            if (all_cats and c.get("game_id") not in excluded_games) or (gid and c.get("game_id") == gid):
                upsert(st, c, now, gid)
                found += 1
        st["last_discovery"] = {"at": S.iso(now), "total": len(clips), "matched": found}
        for cid in args.include_clip or []:
            c = tw.clip(cid)
            if c:
                upsert(st, c, now, gid)
        cat = "все категории" + (" (без просмотра фильмов/аниме)" if excluded_games else "") \
            if all_cats else f"категория «{C.CATEGORY}»"
        rep.lines.append(f"Поиск: {len(clips)} клипов канала за {C.DISCOVERY_HOURS} ч, подходят ({cat}): {found}.")
    rep.lines.append(f"Эфир: {'идёт' if live else 'офлайн'}.")
    st["plan_found"] = found
    if not args.clip_url and C.VOD_SCAN:
        from . import vod as V
        try:
            vods = V.list_vods(tw, bid)
        except Exception as e:
            vods = []
            rep.notes.append(f"список записей недоступен: {e}")
        lim = now - timedelta(hours=24)
        keep = {}
        for i, v in enumerate(vods[:2]):
            if i > 0 and S.parse_ts(v["created_at"]) < lim:
                continue
            sv = st.setdefault("vods", {}).get(v["id"], {})
            sv.update({"duration": v["duration"], "created_at": v["created_at"],
                       "live": bool(live and i == 0), "viewable": v["viewable"]})
            keep[v["id"]] = sv
        st["vods"] = {**{k: x for k, x in st.get("vods", {}).items() if k in keep or x.get("analyzed_until")}, **keep}
        if keep:
            v0 = next(iter(keep.items()))
            rep.lines.append(f"Запись: {V.vod_url(v0[0])} — {v0[1]['duration'] // 60} мин, разобрано до "
                             f"{(v0[1].get('analyzed_until') or 0) // 60} мин{' (эфир идёт)' if v0[1]['live'] else ''}.")
    return found


# ---------- фаза work ----------

def verify_same_event(st, a, b, work_dir, rep):
    """Сравнение двух клипов с близким временем создания по звуку и репликам."""
    from . import media as M
    ra, rb = st["clips"][a], st["clips"][b]
    try:
        pa = M.download(ra["url"], os.path.join(work_dir, a))
        pb = M.download(rb["url"], os.path.join(work_dir, b))
        sim = M.audio_similarity(pa, pb)
    except Exception as e:  # временная ошибка — сравним в следующий раз
        rep.notes.append(f"сравнение {a} и {b} не удалось: {e}")
        return None
    tsim = M.text_similarity((ra.get("transcript") or {}).get("text"),
                             (rb.get("transcript") or {}).get("text"))
    same = sim >= 0.75 or tsim >= 0.35
    st["checked_pairs"].append("|".join(sorted((a, b))))
    if same:
        st["same_event"].append(sorted((a, b)))
    rep.notes.append(f"сравнение {a[:16]}… и {b[:16]}…: звук {sim:.2f}, реплики {tsim:.2f} → "
                     f"{'один момент' if same else 'разные'}")
    return same


class SourceUnavailable(Exception):
    """Метаданные клипа есть, а видео получить не удалось (yt-dlp)."""


def get_source(rec, d, rep=None):
    """Видео для оценки и монтажа.

    * момент из записи — отрезок записи;
    * клип с известным местом в записи — отрезок записи вокруг клипа (подводка + момент + реакция,
      ≈30–40 с), если запись доступна; иначе сам клип;
    * остальное — сам клип.
    """
    from . import media as M
    from . import vod as V
    if rec.get("kind") == "vod":
        s = max(0, rec["vod_offset"] - 3)
        e = rec["vod_offset"] + rec["duration"] + 3
        try:
            path = V.download_section(rec["video_id"], s, e, os.path.join(d, "vod_section.mp4"))
        except M.MediaError as ex:
            raise SourceUnavailable(str(ex)) from None
        rec["source"] = {"kind": "vod", "start": s, "end": e}
        return path
    if C.VOD_CONTEXT and rec.get("video_id") and rec.get("vod_offset") is not None:
        dur = float(rec.get("clip_duration") or rec.get("duration") or 0)
        if dur < C.CLIP_MIN_SEC:
            s, e = V.context_window(float(rec["vod_offset"]), dur)
            try:
                path = V.download_section(rec["video_id"], s, e, os.path.join(d, "vod_context.mp4"))
                rec["source"] = {"kind": "vod_context", "start": s, "end": e}
                return path
            except M.MediaError as ex:
                if rep:
                    rep.notes.append(f"{rec['id']}: запись недоступна ({str(ex)[:120]}) — берём сам клип")
    try:
        path = M.download(rec["url"], d)
    except M.MediaError as ex:
        raise SourceUnavailable(str(ex)) from None
    rec["source"] = {"kind": "clip"}
    return path


JUNK_WORDS = {"XD", "LOL", "KEK", "KEKW", "LUL", "OMEGALUL", "POG", "POGGERS", "W", "L", "GG", "ЛОЛ", "КЕК", "АХАХ"}


def clean_title(t, max_words=7):
    """Фраза из названия клипа на Twitch без мусора («XDDD», «222222», «WW», «ХВХВХВ»)."""
    import re
    t = re.sub(r"[^\w\s!?,.:-]", " ", t or "", flags=re.U)
    words = []
    for w in t.split():
        core = re.sub(r"[^\w]", "", w, flags=re.U)
        letters = re.sub(r"[\d_]", "", core)
        up = letters.upper()
        if not letters or (len(up) >= 2 and len(set(up)) == 1) or (len(up) >= 5 and len(set(up)) <= 2) \
                or re.fullmatch(r"[XХ]+[DД]+", up) or up in JUNK_WORDS:
            continue  # без букв, «WW», «XDDD», «ХВХВХВ», чатовые междометия
        words.append(w)
    if not any(len(re.sub(r"[^\w]", "", w)) >= 3 for w in words):
        return ""
    return " ".join(words[:max_words]).upper().rstrip(".:,")


def evaluate(st, rec, prelim, work_dir, rep):
    """Загрузка, Whisper, кадры, вебка/таймер, оценка Claude или по метаданным, окно монтажа."""
    import cam_detect
    from . import claude_score as CS
    from . import media as M
    from . import transcribe as T

    d = os.path.join(work_dir, rec["id"])
    rec.setdefault("clip_duration", rec.get("duration"))
    if rec.get("duration") and float(rec.get("clip_duration") or rec["duration"]) < C.MIN_CLIP_SEC:
        rec.update(status="rejected", reject_reason=f"клип короче {C.MIN_CLIP_SEC:.0f} с ({rec['duration']:.1f} с)",
                   score=None, window=None)
        return None
    src = get_source(rec, d, rep)
    dur = M.duration(src)
    rec["duration"] = round(dur, 2)
    if dur < C.MIN_CLIP_SEC:
        rec.update(status="rejected", reject_reason=f"клип короче {C.MIN_CLIP_SEC:.0f} с ({dur:.1f} с)",
                   score=None, window=None)
        return src
    try:
        tr = T.transcribe(src)
        rec["transcript"] = tr
        rec["language"] = tr["language"]
        rec.pop("whisper_error", None)
    except Exception as e:
        tr = None
        rec["transcript"] = None
        rec["whisper_error"] = f"{type(e).__name__}: {e}"[:300]
    frames = M.extract_frames(src, os.path.join(d, "frames"), dur)
    rec["frames_sheet"] = M.contact_sheet(frames, os.path.join(d, "frames.jpg"))
    cam, cam_info = cam_detect.detect(src)
    rec["cam"] = list(cam) if cam else None
    if cam:
        has_timer, tbox, _ = cam_detect.detect_timer(src, cam)
    else:
        has_timer, tbox = False, None
    rec["timer_box"] = tbox
    rec["contest_ok"] = bool(has_timer)
    p = prelim.get(rec["id"]) or {"vel_pct": 0.5, "cluster": 0.0, "fresh": 0.5, "views_points": 5}
    rec["views_points"] = p["views_points"]
    prof = C.PROFILE
    rec["title_fallback"] = clean_title(rec.get("title"))
    pr = M.probe(src)
    vs = next(x for x in pr["streams"] if x["codec_type"] == "video")
    cam_a, game_a = M.activity_per_sec(src, dur, cam, int(vs["width"]), int(vs["height"]))
    speech = M.speech_per_sec(tr, dur)
    result = None
    if CS.available():
        try:
            result = CS.score(frames, tr, {"title": rec.get("title"), "duration": dur}, prof.get("claude_notes", ""))
        except Exception as e:
            rep.notes.append(f"Claude не оценил {rec['id']}: {type(e).__name__}: {e}"[:300])
    if result:
        crit = {k: result[k] for k in ("strength", "clarity", "reaction", "hook", "vertical")}
        rec.update({"score_mode": "claude", "criteria": crit, "moment_type": result["moment_type"],
                    "reason": result["reason"], "title": result["title"].strip().upper(),
                    "title_accent": result["title_accent"].strip().upper()})
        # популярность на Twitch — 40 баллов, оценка Claude по кадрам и речи — 60
        rec["score"] = int(round(0.6 * sum(crit.values()) / 90 * 100 + 40 * p.get("popularity", 0.5)))
        s0, e0 = float(result["start_sec"]), float(result["end_sec"])
        # законченный момент короче 30 с оставляем коротким
        rec["window"] = [max(0.0, s0), min(dur, e0)] if 8 <= e0 - s0 < C.CLIP_MIN_SEC and dur > C.CLIP_MIN_SEC \
            and result.get("context_sufficient") else list(S.fit_window(s0, e0, dur))
        rec["leads"] = bool(result.get("streamer_leads_scene"))
        rec["excluded"] = bool(result.get("excluded_content"))
        rec["context_ok"] = bool(result.get("context_sufficient"))
        pk = result.get("reaction_peak_sec", -1)
        rec["zoom_at"] = float(pk) if prof.get("cam_zoom") and cam and pk is not None and pk >= 0 else None
    else:
        win = S.choose_window(speech, cam_a, game_a, dur)
        sig = M.content_signals(win, speech, cam_a, game_a, dur)
        rec.update({"score_mode": "meta", "moment_type": "other", "criteria": None, "signals": sig,
                    "score": S.meta_total(p, sig), "title": None, "title_accent": None,
                    "window": [win[0], win[1]], "leads": None, "excluded": None,
                    "context_ok": sig["context"] >= 0.5})
        parts = [f"{p.get('views', rec.get('views', 0))} просмотров на Twitch "
                 f"({int(p['vel_pct'] * 100)}-й перцентиль среди клипов того же возраста"
                 + ("; нейтрально: ≤1 просмотра" if p.get("neutral_views") else "") + ")"]
        parts.append(f"этот момент клипнули {p.get('cluster_size', 1)} зрител"
                     + ("ь" if p.get("cluster_size", 1) == 1 else "я" if p.get("cluster_size", 1) < 5 else "ей"))
        parts.append(f"реплики {int(sig['speech'] * 100)}%, реакция {sig['reaction']:.2f}, "
                     f"действие {sig['action']:.2f}, контекст {sig['context']:.2f}")
        parts.append(f"возраст {p.get('age_h', 0):.1f} ч")
        rec["reason"] = "Популярность на Twitch: " + ", ".join(parts)
        if rec.get("kind") == "vod":
            rec["reason"] = (f"Момент найден по записи стрима ({', '.join(rec.get('vod_reasons') or [])}); "
                             f"реплики {int(sig['speech'] * 100)}%, реакция {sig['reaction']:.2f}, действие {sig['action']:.2f}")
        elif (rec.get("source") or {}).get("kind") == "vod_context":
            rec["reason"] += "; ролик дополнен контекстом из записи стрима"
        rec["zoom_at"] = S.reaction_peak(cam_a, win) if prof.get("cam_zoom") and cam else None
    # правила канала: кто ведёт сцену, исключённый контент, контекст, отсутствие вебки
    reject = None
    if rec.get("excluded"):
        reject = "реакция на фильм/аниме или фоновый видеоряд"
    elif prof["login"] != "strogo" and rec["score_mode"] == "claude" and rec.get("leads") is False:
        reject = f"{prof['name']} не ведёт сцену"
    elif rec.get("context_ok") is False and rec["score_mode"] == "claude":
        reject = "не хватает контекста для вертикального ролика"
    elif not cam and prof.get("no_cam") == "full_if_lead" and not (rec["score_mode"] == "claude" and rec.get("leads")):
        reject = f"вебка не найдена, а роль {prof['name']} по исходнику не подтверждена"
    rec["reject_reason"] = reject
    rec["status"] = "rejected" if reject else "scored"
    rec["evaluated_at"] = S.iso(datetime.now(timezone.utc))
    return src


def do_render(st, rec, work_dir, out_dir, rep):
    from .render import render_clip
    src = get_source(rec, os.path.join(work_dir, rec["id"]), rep)
    res = render_clip(src, rec, os.path.join(out_dir, rec["id"]), rec.get("transcript"))
    if not res["ok"]:
        raise RuntimeError(f"ffprobe не прошёл: {res['checks']}")
    if rec.get("frames_sheet") and os.path.exists(rec["frames_sheet"]):
        shutil.copy(rec["frames_sheet"], os.path.join(out_dir, rec["id"], "frames.jpg"))
    return res


def scan_vod(st, rep, now, work_dir, store, chunk=None, force_vod=None):
    """Один кусок записи за запуск: звук → Whisper → пики → кандидаты-«моменты записи»."""
    from . import transcribe as T
    from . import vod as V
    items = sorted((st.get("vods") or {}).items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)
    for vid, sv in items:
        if force_vod and vid != force_vod:
            continue
        ch = chunk or V.pending_chunk({"duration": sv.get("duration", 0)}, sv.get("live"), sv)
        if not ch:
            continue
        s, e = ch
        d = os.path.join(work_dir, f"vod-{vid}")
        audio = V.download_section(vid, s, e, os.path.join(d, f"audio_{int(s)}_{int(e)}.m4a"), audio_only=True)
        try:
            tr = T.transcribe(audio)
        except Exception as ex:
            tr = None
            rep.notes.append(f"запись {vid}: Whisper не сработал ({ex}) — поиск по звуку без реплик")
        offs = [float(r["vod_offset"]) for r in st["clips"].values()
                if r.get("video_id") == vid and r.get("vod_offset") is not None and r.get("kind") != "vod"]
        moments = V.find_moments(audio, tr, offs, base=s)
        added = []
        for m in moments:
            rec = V.moment_record(C.CHANNEL, {"id": vid, "created_at": sv["created_at"]}, m)
            if rec["id"] not in st["clips"]:
                st["clips"][rec["id"]] = rec
                store.touched.add(rec["id"])
                added.append(rec)
        if not chunk:
            sv["analyzed_until"] = e
        rep.lines.append(f"Разбор записи {V.vod_url(vid)}: {int(s) // 60}–{int(e) // 60} мин, "
                         f"найдено моментов: {len(added)}"
                         + (" — " + "; ".join(f"{int(r['vod_offset']) // 60}:{int(r['vod_offset']) % 60:02d} "
                                              f"({', '.join(r['vod_reasons'])})" for r in added) if added else ""))
        store.save(st, f"vod {vid}: разобрано до {int(e)} с")
        return added
    return []


def _clips(st, now, args):
    clips = window_clips(st, now)
    m = (st.get("manual") or {}).get("id")
    for cid in ([m] if m else []) + (args.include_clip or []):
        if cid in st["clips"]:
            clips[cid] = st["clips"][cid]
    return clips


def _group_members(clips, st):
    g = S.group_clips(clips, st["same_event"])
    mem = {}
    for cid, gid in g.items():
        mem.setdefault(gid, []).append(cid)
    return g, mem


def phase_work(args, st, rep, now, work_dir, out_dir, store):
    manual = dict(st.get("manual") or {})
    mid = manual.get("id")
    today = S.local_day(now)
    clips = _clips(st, now, args)
    groups, members = _group_members(clips, st)
    prelim = S.prelim_scores(clips, groups, now)

    # 1) исключаем уже обработанные clip_id и клипы уже смонтированных моментов
    for gid, ids in members.items():
        done = [i for i in ids if clips[i]["status"] in ST.RENDER_ACTIVE]
        for i in ids:
            r = clips[i]
            if r["status"] in ST.PROCESSED:
                rep.data["excluded_processed"].append(i)
                continue
            if done and i != mid and r["status"] in ST.OPEN | {"scored", "reserve"}:
                r["status"], r["dup_of"] = "duplicate", done[0]
                store.touched.add(i)
                rep.data["duplicates"].append(i)
                rep.row(r, "дубль уже смонтированного момента", done[0][:16])

    # 1б) разбор нового куска записи стрима (в т. ч. во время эфира)
    if C.VOD_SCAN and not mid:
        try:
            scan_vod(st, rep, now, work_dir, store)
        except Exception as e:
            rep.notes.append(f"разбор записи не удался: {type(e).__name__}: {e}"[:300])
            traceback.print_exc()
        clips = _clips(st, now, args)
        groups, members = _group_members(clips, st)
        prelim = S.prelim_scores(clips, groups, now)

    # 2) кандидаты на подробную оценку — представители групп
    cands = []
    for ids in members.values():
        open_ids = [i for i in ids if clips[i]["status"] in ST.OPEN and i != mid
                    and float(clips[i].get("duration") or 99) >= C.MIN_CLIP_SEC]
        if open_ids and not any(clips[i]["status"] in ST.RENDER_ACTIVE | {"scored", "reserve"} for i in ids):
            cands.append(S.representative(open_ids, clips))
    used = ST.day(st, today)["evaluated"]
    allowance = S.eval_allowance(used, now)
    picked = S.pick_for_eval(cands, prelim, min(C.MAX_EVAL_PER_RUN, allowance))
    skip_eval = set()
    if mid:
        mrec = st["clips"][mid]
        if mrec["status"] in ST.RENDER_ACTIVE and not manual.get("force"):
            rep.data["skipped"].append(mid)
            rep.row(mrec, "уже смонтирован — повторно не ставим", "используйте force для пересборки")
            mid = None
        else:
            if manual.get("force"):
                mrec["force_run"] = store.run_id  # явная пересборка: снимает защиту «смонтирован»
                store.touched.add(mid)
            elif mrec["status"] in {"scored", "reserve"} and mrec.get("window"):
                skip_eval.add(mid)  # уже оценён — без повторной оценки
            elif mrec["status"] != "new":
                mrec["status"] = "new"
                store.touched.add(mid)
            picked = [mid] + picked
    rep.lines.append(f"Сутки {today} (Asia/Yerevan): подробных оценок {used}/{C.DAILY_EVAL_BUDGET}, "
                     f"доступно сейчас {allowance}; кандидатов на оценку {len(cands)}, взято {len(picked)}.")
    store.save(st, "work: отсев обработанных")

    # 3) подробная оценка (с проверкой клипов, созданных рядом по времени)
    for cid in picked:
        if cid in skip_eval:
            continue
        allowed = set(ST.OPEN) | ({st["clips"][cid]["status"]} if cid == mid else set())
        if not store.claim(st, cid, allowed):
            rep.notes.append(f"{cid}: уже взят другим запуском — пропуск")
            continue
        clips = _clips(st, now, args)
        dup = None
        for x, y in S.proximity_pairs(clips, st["checked_pairs"]):
            if cid not in (x, y):
                continue
            other = y if x == cid else x
            if verify_same_event(st, cid, other, work_dir, rep):
                g, mem = _group_members(_clips(st, now, args), st)
                done = [i for i in mem[g[cid]] if st["clips"][i]["status"] in ST.RENDER_ACTIVE]
                if done and cid != mid:
                    dup = done[0]
                    break
        rec = st["clips"][cid]
        if dup:
            rec["status"], rec["dup_of"] = "duplicate", dup
            rep.data["duplicates"].append(cid)
            rep.row(rec, "дубль уже смонтированного момента", dup[:16])
        else:
            try:
                evaluate(st, rec, prelim, work_dir, rep)
                rep.data["evaluated"].append(cid)
                if rec.get("whisper_error"):
                    rep.notes.append(f"{cid}: сбой Whisper ({rec['whisper_error']}) — отбор без учёта реплик")
                if rec["status"] == "rejected":
                    rep.row(rec, "отклонён", rec.get("reject_reason") or "")
            except SourceUnavailable as e:
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["source_error"] = str(e)[:300]
                rec["status"] = "source_retry" if rec["attempts"] < C.MAX_ATTEMPTS else "source_unavailable"
                rep.data.setdefault("source_unavailable", []).append(cid)
                rep.row(rec, "исходное видео недоступно" + (", повтор позже" if rec["status"] == "source_retry" else ""),
                        rec["source_error"])
            except Exception as e:
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["last_error"] = f"{type(e).__name__}: {e}"[:300]
                rec["status"] = "eval_retry" if rec["attempts"] < C.MAX_ATTEMPTS else "eval_failed"
                rep.data["errors"].append(cid)
                rep.row(rec, "ошибка оценки, повтор позже" if rec["status"] == "eval_retry"
                        else "ошибка оценки (попытки исчерпаны)", rec["last_error"])
                traceback.print_exc()
        store.release(st, cid)
        store.save(st, f"work: оценка {cid}")

    # 4) дневная подборка
    if mid and st["clips"][mid]["status"] in {"scored", "reserve"} | ST.RENDER_ACTIVE:
        st["clips"][mid]["status"] = "render_queued"
        st["clips"][mid]["manual"] = True
        store.touched.add(mid)
    clips = _clips(st, now, args)
    by_day = {}
    for r in clips.values():
        if r["status"] in {"scored", "reserve"} | ST.RENDER_ACTIVE:
            by_day.setdefault(S.local_day(r["created_at"]), []).append(r)
    for dkey, recs in sorted(by_day.items()):
        finalize = dkey < today or S.should_finalize(st["stream"], now)
        view = [{"id": r["id"], "status": r["status"], "score": r.get("score"),
                 "score_mode": r.get("score_mode", "meta"), "moment_type": r.get("moment_type") or "other"}
                for r in recs]
        pace = S.render_pace(now) if C.CHANNEL in C.NEW_CHANNELS and dkey == today and not finalize else None
        for cid, status in S.decide_day(view, finalize, C.MAX_PER_DAY, pace).items():
            st["clips"][cid]["status"] = status
            store.touched.add(cid)
        ST.day(st, dkey)["finalized"] = finalize
    for cid in rep.data["evaluated"]:
        r = st["clips"][cid]
        if r["status"] not in ("render_queued", "rejected"):
            rep.row(r, {"reserve": "резерв"}.get(r["status"], r["status"]))
    store.save(st, "work: дневная подборка")

    # 5) монтаж — клип готов только после успешного рендера
    queue = [r for r in _clips(st, now, args).values() if r["status"] in ("render_queued", "render_retry")]
    queue.sort(key=lambda r: (not r.get("manual"), -(r.get("score") or 0)))
    rep.data["render_queue"] = [r["id"] for r in queue]
    if args.dry_run:
        for r in queue:
            rep.row(r, "в монтаж (dry run — без рендера)")
        queue = []
    for qid in [r["id"] for r in queue[: C.MAX_RENDER_PER_RUN]]:
        def group_free(remote, qid=qid):
            cl = {k: v for k, v in remote["clips"].items() if k in clips or k == qid}
            g, mem = _group_members(cl, remote)
            return not any(remote["clips"][i]["status"] == "rendered" for i in mem.get(g.get(qid), []) if i != qid)
        allowed = {"render_queued", "render_retry"} | ({"rendered"} if qid == mid and manual.get("force") else set())
        if not store.claim(st, qid, allowed, group_free):
            rep.notes.append(f"{qid}: уже смонтирован или взят другим запуском — пропуск")
            continue
        r = st["clips"][qid]
        try:
            res = do_render(st, r, work_dir, out_dir, rep)
            r["status"] = "rendered"
            r["rendered_at"] = S.iso(datetime.now(timezone.utc))
            r["output"] = {"mp4": os.path.relpath(res["mp4"], out_dir),
                           "srt": os.path.relpath(res["srt"], out_dir) if res["srt"] else None,
                           "checks": res["checks"]}
            rep.data["rendered"].append(qid)
            extra = (f"ffprobe {res['checks']['resolution']}, {res['checks']['duration']} с, "
                     f"звук {'есть' if res['checks']['audio'] else 'нет'}; заголовок «{res['meta']['title']}»"
                     + (f"; размыто участков: {res['checks']['blurred_regions']}" if res['checks'].get('blurred_regions') else "")
                     + ((f"; конкурс: {'таймер в кадре' if r.get('contest_ok') else 'НЕПРИГОДЕН (таймера нет)'}")
                        if C.PROFILE.get("contest_timer") else "")
                     + f"; файлы: {os.path.basename(res['mp4'])} + .txt/.json")
            rep.row(r, "✅ смонтирован", extra)
            for n in res["notes"]:
                rep.notes.append(f"{qid}: {n}")
        except Exception as e:
            r["attempts"] = r.get("attempts", 0) + 1
            r["last_error"] = f"{type(e).__name__}: {e}"[:300]
            r["status"] = "render_retry" if r["attempts"] < C.MAX_ATTEMPTS else "render_failed"
            rep.data["failed"].append(qid)
            rep.row(r, "ошибка монтажа, повтор позже" if r["status"] == "render_retry"
                    else "ошибка монтажа (попытки исчерпаны)", r["last_error"])
            traceback.print_exc()
        if r["status"] == "rendered":
            r.pop("manual", None)
            r.pop("force_run", None)
        store.release(st, qid)
        store.save(st, f"work: монтаж {qid}")

    # итоги суток
    st.pop("manual", None)
    d = ST.day(st, today)
    rendered_today = [c for c, r in st["clips"].items()
                      if r["status"] == "rendered" and S.local_day(r["created_at"]) == today]
    reserve = [c for c, r in st["clips"].items()
               if r["status"] == "reserve" and S.local_day(r["created_at"]) == today]
    rep.lines.append(f"Подборка {today}: готово {len(rendered_today)} из цели {C.TARGET_PER_DAY} "
                     f"(не больше {C.MAX_PER_DAY}), в резерве {len(reserve)}, "
                     f"итог суток {'подведён' if d.get('finalized') else 'после эфира'}.")


def needs_work(st, now):
    if st.get("manual"):
        return True
    clips = window_clips(st, now)
    if any(r["status"] in ("render_queued", "render_retry") for r in clips.values()):
        return True
    open_ = [r for r in clips.values() if r["status"] in ST.OPEN]
    if open_ and S.eval_allowance(ST.day(st, S.local_day(now))["evaluated"], now) > 0:
        return True
    if C.VOD_SCAN:
        from . import vod as V
        for vid, sv in (st.get("vods") or {}).items():
            if sv.get("duration") and V.pending_chunk({"duration": sv["duration"]}, sv.get("live"), sv):
                return True
    # добор после эфира из резерва
    return any(r["status"] in ("scored", "reserve") for r in clips.values()) and S.should_finalize(st["stream"], now)


def phase_test(args, st, rep, now, work_dir, out_dir):
    """Тест канала: до --count свежих подходящих клипов разных моментов → видео → монтаж → артефакт.

    Без записи состояния и без расхода дневного лимита. Исходы различаются:
    no_clips (нет подходящих), source_unavailable (видео не получено), render_error, ok.
    """
    count = max(1, args.count)
    res = {"channel": C.CHANNEL, "found": st.get("plan_found", 0), "source": None,
           "outcome": "no_clips", "detail": "", "mp4": None, "clips": []}
    if args.source == "vod":
        # моменты из последних минут свежей записи стрима
        from . import vod as V
        vods = sorted((st.get("vods") or {}).items(), key=lambda kv: kv[1].get("created_at", ""), reverse=True)
        if not vods:
            res["detail"] = "у канала нет доступных записей"
            rep.lines.append(f"**Тест записи {C.PROFILE['name']} ({C.CHANNEL}):** записей нет")
            rep.data["test"] = res
            return res
        vid, sv = vods[0]
        edge = sv["duration"] - (V.LIVE_EDGE_SEC if sv.get("live") else 0)
        span = args.vod_minutes * 60
        try:
            added = scan_vod(st, rep, now, work_dir, _NoStore(), chunk=(max(0, edge - span), edge), force_vod=vid)
        except Exception as e:
            res.update(outcome="source_unavailable", source=V.vod_url(vid), detail=f"{type(e).__name__}: {e}"[:300])
            rep.lines.append(f"**Тест записи {C.PROFILE['name']}:** запись недоступна — {res['detail']}")
            rep.data["test"] = res
            return res
        res["found"] = len(added)
        for r in added:
            r["status"] = "new"
    clips = window_clips(st, now, hours=C.DISCOVERY_HOURS)
    if args.source == "vod":
        clips = {k: v for k, v in clips.items() if v.get("kind") == "vod"}
    groups = S.group_clips(clips, st["same_event"])
    prelim = S.prelim_scores(clips, groups, now)
    exclude = set(args.exclude_clip or [])
    excluded_groups = {groups[c] for c in exclude if c in groups}
    fresh = [c for c in clips if clips[c]["status"] not in ST.RENDER_ACTIVE and c not in exclude
             and groups[c] not in excluded_groups and float(clips[c].get("duration") or 99) >= C.MIN_CLIP_SEC]
    # по одному лучшему клипу на момент, по убыванию популярности
    best = {}
    for c in sorted(fresh, key=lambda c: -prelim[c]["prelim"]):
        best.setdefault(groups[c], c)
    order = sorted(best.values(), key=lambda c: -prelim[c]["prelim"])
    tried, reasons, done = 0, [], 0
    labels = {"ok": "✅ готово", "no_clips": "нет подходящих клипов", "source_unavailable": "исходное видео недоступно",
              "render_error": "ошибка монтажа", "eval_error": "ошибка оценки"}
    for cid in order[: count * 3 + 2]:
        if done >= count:
            break
        rec = st["clips"][cid]
        tried += 1
        try:
            evaluate(st, rec, prelim, work_dir, rep)
        except SourceUnavailable as e:
            reasons.append(f"{cid}: исходное видео недоступно — {e}")
            if not done:
                res.update(outcome="source_unavailable", source=rec["url"], detail=str(e)[:300])
            continue
        except Exception as e:
            reasons.append(f"{cid}: ошибка оценки — {type(e).__name__}: {e}")
            continue
        if rec["status"] == "rejected":
            reasons.append(f"{cid}: отклонён — {rec.get('reject_reason')}")
            continue
        item = {"source": rec["url"], "score": rec.get("score"), "window": rec.get("window")}
        try:
            r = do_render(st, rec, work_dir, out_dir, rep)
            item.update(outcome="ok", mp4=os.path.relpath(r["mp4"], out_dir), title=r["meta"]["title"],
                        checks=r["checks"])
            done += 1
            res.update(outcome="ok", source=res["source"] or rec["url"], mp4=res["mp4"] or item["mp4"])
            rep.row(rec, "✅ смонтирован (тест)", f"ffprobe {r['checks']['resolution']}, {r['checks']['duration']} с, "
                    f"звук {'есть' if r['checks']['audio'] else 'нет'}; заголовок «{r['meta']['title']}»"
                    + (f"; размыто участков: {r['checks']['blurred_regions']}" if r['checks'].get('blurred_regions') else ""))
            for n in r["notes"]:
                rep.notes.append(f"{cid}: {n}")
        except Exception as e:
            item.update(outcome="render_error", detail=f"{type(e).__name__}: {e}"[:300])
            if not done:
                res.update(outcome="render_error", source=rec["url"], detail=item["detail"])
            rep.row(rec, "ошибка монтажа (тест)", item["detail"])
            traceback.print_exc()
        res["clips"].append(item)
    if res["outcome"] == "no_clips" and reasons:
        res["detail"] = "; ".join(reasons)
    rep.lines.append(f"**Тест {'записи ' if args.source == 'vod' else ''}{C.PROFILE['name']} ({C.CHANNEL}):** "
                     f"найдено {res['found']}, проверено {tried}, "
                     f"готово {done} из {count}, итог: **{labels.get(res['outcome'], res['outcome'])}**"
                     + (f" — {res['detail']}" if res["detail"] and res["outcome"] != "ok" else ""))
    for r_ in reasons:
        rep.notes.append(r_)
    rep.data["test"] = res
    return res


class _NoStore:
    """Хранилище-заглушка для теста: ничего не пишет."""
    touched = set()

    def save(self, st, msg=""):
        return st


def resolve_clip_channel(clip_url):
    from .twitch import Twitch, clip_id_from_url
    cid = clip_id_from_url(clip_url) or clip_url.strip()
    clip = Twitch().clip(cid)
    if not clip:
        raise RuntimeError(f"клип {cid} не найден в Twitch Helix")
    name = (clip.get("broadcaster_name") or "").lower()
    for login in C.PROFILES:
        if login == name:
            return login
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["plan", "work", "all", "test"], default="all")
    ap.add_argument("--channels", default="", help="strogo | evelone2004 | zloyn | new | all (пусто — все)")
    ap.add_argument("--category", default="default", help="название категории или all")
    ap.add_argument("--state", default="state/state.json", help="папка состояния = dirname(--state)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--work", default="work/pipeline")
    ap.add_argument("--clip-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--include-clip", action="append")
    ap.add_argument("--exclude-clip", action="append", help="тест: не брать эти клипы и их моменты")
    ap.add_argument("--count", type=int, default=1, help="тест: сколько роликов на канал")
    ap.add_argument("--source", choices=["clips", "vod"], default="clips", help="тест: клипы или моменты записи")
    ap.add_argument("--vod-minutes", type=int, default=15, help="тест записи: сколько последних минут разобрать")
    ap.add_argument("--now", help="ISO-время для тестов")
    a = ap.parse_args(argv)

    from .store import Store
    now = S.parse_ts(a.now) if a.now else datetime.now(timezone.utc)
    os.makedirs(a.out, exist_ok=True)
    state_dir = os.path.dirname(a.state) or "."
    test = a.phase == "test"
    code = 0
    try:
        channels = C.channels_from_arg(a.channels if a.channels or not test else "new")
        if a.clip_url and a.phase in ("plan", "all"):
            owner = resolve_clip_channel(a.clip_url)
            channels = [owner] if owner else channels[:1]
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return 1
    mode = "оценка Claude" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "отбор по метаданным"
    any_work, total_rendered, combined = False, 0, {}
    head = (f"## Clip Pipeline{' — тест новых каналов' if test else ''}\n\nРежим: **{mode}**"
            f"{' · dry run (без рендера и без записи состояния)' if a.dry_run else ''}"
            f"{' · тест: без записи состояния и без расхода лимита' if test else ''}"
            f" · {now.astimezone(S.YEREVAN):%Y-%m-%d %H:%M} (Ереван)")
    gs = os.environ.get("GITHUB_STEP_SUMMARY")
    if gs and a.phase != "work":
        with open(gs, "a", encoding="utf-8") as fh:
            fh.write(head + "\n\n")
    for login in channels:
        prof = C.apply_profile(login, a.category)
        path = os.path.join(state_dir, prof["state_file"])
        store = Store(path, login, remote=os.environ.get("STATE_REMOTE") or None,
                      branch=os.environ.get("STATE_BRANCH", "pipeline-state"), readonly=a.dry_run or test)
        rep = Report()
        out_dir = os.path.join(a.out, login)
        work_dir = os.path.join(a.work, login)
        try:
            st = store.load()
            if a.phase in ("plan", "all", "test"):
                phase_plan(a, st, rep, now)
                nw = needs_work(st, now)
                any_work = any_work or nw
                if not test:
                    rep.lines.append("Нужна оценка/монтаж: " + ("да" if nw else "нет — новых кандидатов и очереди нет"))
                    st["runs"] = (st.get("runs") or []) + [{"at": S.iso(now), "phase": "plan", "run": store.run_id}]
                    st["updated_at"] = S.iso(now)
                    store.save(st, f"plan {login}: поиск и история просмотров")
                    if a.dry_run:
                        ST.save(path + ".dry", st)
            if test:
                phase_test(a, st, rep, now, work_dir, out_dir)
            elif a.phase in ("work", "all"):
                if a.dry_run and os.path.exists(path + ".dry"):
                    st = ST.load(path + ".dry", login)
                elif a.phase == "work":
                    st = store.load()
                if a.phase == "work" and not needs_work(st, now):
                    rep.lines.append("Работы нет.")
                else:
                    phase_work(a, st, rep, now, work_dir, out_dir, store)
                    st["runs"] = (st.get("runs") or []) + [{"at": S.iso(now), "phase": "work", "run": store.run_id,
                                                             "rendered": rep.data["rendered"]}]
                    store.save(st, f"work {login}: итог")
        except Exception as e:
            code = 1
            rep.lines.append(f"❌ **Ошибка:** {type(e).__name__}: {e}")
            traceback.print_exc()
        total_rendered += len(rep.data["rendered"]) + (1 if (rep.data.get("test") or {}).get("outcome") == "ok" else 0)
        combined[login] = rep.data
        rep.write(os.path.join(a.out, f"summary_{a.phase}_{login}.md"),
                  os.path.join(a.out, f"run_{a.phase}_{login}.json"),
                  f"### {prof['name']} ({login}) — {'тест' if test else a.phase}")
    with open(os.path.join(a.out, f"run_{a.phase}.json"), "w", encoding="utf-8") as fh:
        json.dump(combined, fh, ensure_ascii=False, indent=1)
    gh_output(needs_work=str(any_work).lower(), rendered=total_rendered, channels=",".join(channels))
    return code


if __name__ == "__main__":
    sys.exit(main())
