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
        "duration": float(clip.get("duration") or 0), "game_id": clip.get("game_id"),
        "slug": cid,
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
    gid = tw.game_id(C.CATEGORY)
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
        for c in clips:
            if gid and c.get("game_id") == gid:
                upsert(st, c, now, gid)
                found += 1
        for cid in args.include_clip or []:
            c = tw.clip(cid)
            if c:
                upsert(st, c, now, gid)
        rep.lines.append(f"Поиск: {len(clips)} клипов канала за {C.DISCOVERY_HOURS} ч, "
                         f"из них в категории «{C.CATEGORY}»: {found}.")
    rep.lines.append(f"Эфир: {'идёт' if live else 'офлайн'}.")
    st["plan_found"] = found
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


def evaluate(st, rec, prelim, work_dir, rep):
    """Загрузка, Whisper, кадры, вебка/таймер, оценка Claude или по метаданным, окно монтажа."""
    import cam_detect
    import narezchik
    from . import claude_score as CS
    from . import media as M
    from . import transcribe as T

    d = os.path.join(work_dir, rec["id"])
    src = M.download(rec["url"], d)
    dur = M.duration(src)
    rec["duration"] = round(dur, 2)
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
    result = None
    if CS.available():
        try:
            result = CS.score(frames, tr, {"title": rec.get("title"), "duration": dur})
        except Exception as e:
            rep.notes.append(f"Claude не оценил {rec['id']}: {type(e).__name__}: {e}"[:300])
    if result:
        crit = {k: result[k] for k in ("strength", "clarity", "reaction", "hook", "vertical")}
        rec.update({"score_mode": "claude", "criteria": crit, "moment_type": result["moment_type"],
                    "reason": result["reason"], "title": result["title"].strip().upper(),
                    "title_accent": result["title_accent"].strip().upper()})
        rec["score"] = sum(crit.values()) + p["views_points"]
        rec["window"] = list(S.fit_window(result["start_sec"], result["end_sec"], dur))
    else:
        fit = M.segment_fit(dur, tr["text"] if tr else None)
        rec.update({"score_mode": "meta", "moment_type": "other", "criteria": None,
                    "score": S.meta_total(p, fit), "title": None, "title_accent": None})
        parts = [f"рост просмотров {int(p['vel_pct'] * 100)}-й перцентиль"
                 + (" (нейтрально: у всех ≤1 просмотра)" if p.get("neutral_views") else "")]
        if p.get("cluster_size", 1) > 1:
            parts.append(f"{p['cluster_size']} клипа одного момента")
        parts.append(f"возраст {p.get('age_h', 0):.1f} ч")
        rec["reason"] = "Отбор по метаданным: " + ", ".join(parts)
        rms = narezchik.audio_rms_per_sec(src, dur)
        mot = narezchik.motion_per_sec(src, dur)
        if dur > C.CLIP_MIN_SEC:
            s, e, _ = narezchik.pick_window(rms, mot, int(C.CLIP_MIN_SEC), int(C.CLIP_MAX_SEC))
            rec["window"] = list(S.fit_window(s, min(e, dur), dur))
        else:
            rec["window"] = [0.0, round(dur, 3)]
    rec["status"] = "scored"
    rec["evaluated_at"] = S.iso(datetime.now(timezone.utc))
    return src


def do_render(st, rec, work_dir, out_dir, rep):
    from . import media as M
    from .render import render_clip
    src = M.download(rec["url"], os.path.join(work_dir, rec["id"]))
    res = render_clip(src, rec, os.path.join(out_dir, rec["id"]), rec.get("transcript"))
    if not res["ok"]:
        raise RuntimeError(f"ffprobe не прошёл: {res['checks']}")
    if rec.get("frames_sheet") and os.path.exists(rec["frames_sheet"]):
        shutil.copy(rec["frames_sheet"], os.path.join(out_dir, rec["id"], "frames.jpg"))
    return res


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
            if done and i != mid and r["status"] in ("new", "eval_retry", "scored", "reserve"):
                r["status"], r["dup_of"] = "duplicate", done[0]
                store.touched.add(i)
                rep.data["duplicates"].append(i)
                rep.row(r, "дубль уже смонтированного момента", done[0][:16])

    # 2) кандидаты на подробную оценку — представители групп
    cands = []
    for ids in members.values():
        open_ids = [i for i in ids if clips[i]["status"] in ("new", "eval_retry") and i != mid]
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
            if mrec["status"] in {"scored", "reserve"} | ST.RENDER_ACTIVE and mrec.get("window"):
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
        allowed = {"new", "eval_retry"} | ({st["clips"][cid]["status"]} if cid == mid else set())
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
                    rep.notes.append(f"{cid}: сбой Whisper ({rec['whisper_error']}) — монтаж без субтитров")
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
        for cid, status in S.decide_day(view, finalize).items():
            st["clips"][cid]["status"] = status
            store.touched.add(cid)
        ST.day(st, dkey)["finalized"] = finalize
    for cid in rep.data["evaluated"]:
        r = st["clips"][cid]
        if r["status"] != "render_queued":
            rep.row(r, {"reserve": "резерв", "rejected": "отклонён"}.get(r["status"], r["status"]))
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
                     f"звук {'есть' if res['checks']['audio'] else 'нет'}; субтитры "
                     f"{'есть' if res['srt'] else 'нет'}; конкурс: "
                     f"{'таймер в кадре' if r.get('contest_ok') else 'НЕПРИГОДЕН (таймера нет)'}")
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
        r.pop("manual", None) if r["status"] == "rendered" else None
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
                     f"(диапазон {C.MIN_PER_DAY}–{C.MAX_PER_DAY}), в резерве {len(reserve)}, "
                     f"итог суток {'подведён' if d.get('finalized') else 'после эфира'}.")


def needs_work(st, now):
    if st.get("manual"):
        return True
    clips = window_clips(st, now)
    if any(r["status"] in ("render_queued", "render_retry") for r in clips.values()):
        return True
    open_ = [r for r in clips.values() if r["status"] in ("new", "eval_retry")]
    if open_ and S.eval_allowance(ST.day(st, S.local_day(now))["evaluated"], now) > 0:
        return True
    # добор после эфира из резерва
    return any(r["status"] in ("scored", "reserve") for r in clips.values()) and S.should_finalize(st["stream"], now)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["plan", "work", "all"], default="all")
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--work", default="work/pipeline")
    ap.add_argument("--clip-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--include-clip", action="append")
    ap.add_argument("--now", help="ISO-время для тестов")
    a = ap.parse_args(argv)

    from .store import Store
    now = S.parse_ts(a.now) if a.now else datetime.now(timezone.utc)
    os.makedirs(a.out, exist_ok=True)
    store = Store(a.state, C.CHANNEL, remote=os.environ.get("STATE_REMOTE") or None,
                  branch=os.environ.get("STATE_BRANCH", "pipeline-state"), readonly=a.dry_run)
    st = store.load()
    rep = Report()
    mode = "оценка Claude" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "отбор по метаданным"
    header = (f"## Clip Pipeline — {C.CHANNEL} / {C.CATEGORY}\n\nРежим: **{mode}**"
              f"{' · dry run (без рендера и без записи состояния)' if a.dry_run else ''}"
              f" · {now.astimezone(S.YEREVAN):%Y-%m-%d %H:%M} (Ереван)")
    code = 0
    try:
        if a.phase in ("plan", "all"):
            phase_plan(a, st, rep, now)
            nw = needs_work(st, now)
            gh_output(needs_work=str(nw).lower())
            rep.lines.append("Нужна оценка/монтаж: " + ("да" if nw else "нет — новых кандидатов и очереди нет"))
            st["runs"] = (st.get("runs") or []) + [{"at": S.iso(now), "phase": "plan", "run": store.run_id}]
            st["updated_at"] = S.iso(now)
            store.save(st, "plan: поиск и история просмотров")
            if a.dry_run and a.phase == "plan":
                # dry run не пишет ветку: передаём результат поиска в фазу work через файл
                ST.save(a.state + ".dry", st)
        if a.phase in ("work", "all"):
            if a.dry_run and os.path.exists(a.state + ".dry"):
                st = ST.load(a.state + ".dry", C.CHANNEL)
            elif a.phase == "work":
                st = store.load()
            phase_work(a, st, rep, now, a.work, a.out, store)
            st["runs"] = (st.get("runs") or []) + [{"at": S.iso(now), "phase": "work", "run": store.run_id,
                                                     "rendered": rep.data["rendered"]}]
            store.save(st, "work: итог")
    except Exception as e:
        code = 1
        rep.lines.append(f"❌ **Ошибка:** {type(e).__name__}: {e}")
        traceback.print_exc()
    rep.write(os.path.join(a.out, f"summary_{a.phase}.md"), os.path.join(a.out, f"run_{a.phase}.json"),
              header if a.phase != "work" else "### Оценка и монтаж")
    gh_output(rendered=len(rep.data["rendered"]))
    return code


if __name__ == "__main__":
    sys.exit(main())
