"""Отбор: группировка клипов одного события, оценка по метаданным, бюджет, дневная подборка.

Только стандартная библиотека — модуль покрыт тестами (tests/test_select.py).
"""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config as C
from .state import RENDER_ACTIVE

YEREVAN = ZoneInfo(C.TZ)


# ---------- время ----------

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(ts):
    dt = parse_ts(ts) if isinstance(ts, str) else ts
    return dt.astimezone(YEREVAN).strftime("%Y-%m-%d")


def local_hour(now):
    return now.astimezone(YEREVAN).hour + now.astimezone(YEREVAN).minute / 60


# ---------- просмотры ----------

def record_views(rec, views, now, keep=60):
    hist = rec.setdefault("views_history", [])
    if not hist or hist[-1][1] != views or (now - parse_ts(hist[-1][0])) > timedelta(minutes=30):
        hist.append([iso(now), int(views)])
    del hist[:-keep]
    rec["views"] = int(views)


def velocity(rec, now):
    """Просмотров в час: по истории (≥20 мин между точками), иначе среднее с момента создания."""
    hist = rec.get("views_history") or []
    if len(hist) >= 2:
        t0, v0 = parse_ts(hist[0][0]), hist[0][1]
        t1, v1 = parse_ts(hist[-1][0]), hist[-1][1]
        if (t1 - t0) >= timedelta(minutes=20):
            return max(0.0, (v1 - v0) / ((t1 - t0).total_seconds() / 3600))
    age_h = max(0.25, (now - parse_ts(rec["created_at"])).total_seconds() / 3600)
    return rec.get("views", 0) / age_h


def age_bucket(age_h):
    for i, lim in enumerate((1, 3, 6, 12, 24)):
        if age_h < lim:
            return i
    return 5


# ---------- группировка «один момент» ----------

class _DSU:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _vod_interval(r):
    if r.get("video_id") and r.get("vod_offset") is not None:
        s = float(r["vod_offset"])
        return r["video_id"], s, s + float(r.get("duration") or 0)
    return None


def group_clips(clips, same_event=()):
    """clip_id -> group_id. Пересечение таймкодов VOD + подтверждённые пары."""
    ids = sorted(clips)
    dsu = _DSU(ids)
    by_video = {}
    for cid in ids:
        iv = _vod_interval(clips[cid])
        if iv:
            by_video.setdefault(iv[0], []).append((iv[1], iv[2], cid))
    for items in by_video.values():
        items.sort()
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[j][0] > items[i][1] + C.VOD_TOLERANCE:
                    break
                dsu.union(items[i][2], items[j][2])
    for a, b in same_event:
        if a in dsu.p and b in dsu.p:
            dsu.union(a, b)
    return {cid: dsu.find(cid) for cid in ids}


def proximity_pairs(clips, checked=()):
    """Пары без таймкодов VOD с близким временем создания — их надо сравнить по звуку/репликам."""
    checked = set(checked)
    items = sorted(((parse_ts(r["created_at"]), cid) for cid, r in clips.items()))
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if (items[j][0] - items[i][0]).total_seconds() > C.PROXIMITY_SECONDS:
                break
            a, b = items[i][1], items[j][1]
            ia, ib = _vod_interval(clips[a]), _vod_interval(clips[b])
            if ia and ib and ia[0] == ib[0]:
                continue  # есть таймкоды одного VOD — сравнение по ним уже сделано
            key = "|".join(sorted((a, b)))
            if key not in checked:
                out.append((a, b))
    return out


def representative(member_ids, clips):
    """Версия с наиболее полным событием и реакцией: длиннее, затем позже кончается, затем больше просмотров."""
    def key(cid):
        r = clips[cid]
        iv = _vod_interval(r)
        end = iv[2] if iv else float(r.get("duration") or 0)
        return (float(r.get("duration") or 0), end, r.get("views", 0), cid)
    return max(member_ids, key=key)


# ---------- оценка по метаданным ----------

def prelim_scores(clips, groups, now):
    """Предварительная оценка всех клипов окна.

    vel   — перцентиль скорости роста просмотров среди клипов сопоставимого возраста
            (нейтрально 0.5, если у всех ≤1 просмотра);
    cluster — число независимых клипов одного момента (2+ → выше);
    fresh — свежесть (до 2 ч — максимум, к 24 ч — 0).
    """
    ids = list(clips)
    if not ids:
        return {}
    vel = {cid: velocity(clips[cid], now) for cid in ids}
    ages = {cid: (now - parse_ts(clips[cid]["created_at"])).total_seconds() / 3600 for cid in ids}
    neutral = max(clips[cid].get("views", 0) for cid in ids) <= 1
    buckets = {}
    for cid in ids:
        buckets.setdefault(age_bucket(ages[cid]), []).append(cid)
    sizes = {}
    for cid, g in groups.items():
        sizes[g] = sizes.get(g, 0) + 1
    out = {}
    for cid in ids:
        if neutral:
            vp = 0.5
        else:
            peers = buckets[age_bucket(ages[cid])]
            if len(peers) < 3:
                peers = ids
            vals = sorted(vel[p] for p in peers)
            below = sum(1 for v in vals if v < vel[cid])
            equal = sum(1 for v in vals if v == vel[cid])
            vp = (below + 0.5 * equal) / len(vals)
        n = sizes.get(groups.get(cid, cid), 1)
        cluster = min(1.0, (n - 1) / 3)
        fresh = 1.0 if ages[cid] <= 2 else max(0.0, 1 - (ages[cid] - 2) / 22)
        out[cid] = {
            "vel_pct": round(vp, 3), "cluster": round(cluster, 3), "cluster_size": n,
            "fresh": round(fresh, 3), "age_h": round(ages[cid], 2),
            "prelim": round(0.5 * vp + 0.3 * cluster + 0.2 * fresh, 4),
            # «динамика просмотров» — 10 баллов из 100
            "views_points": int(round(10 * (0.7 * vp + 0.3 * cluster))),
            "neutral_views": neutral,
        }
    return out


def meta_total(p, segment_fit):
    """Отбор по метаданным (без Claude): 0..100 по доступным сигналам."""
    return int(round(40 * p["vel_pct"] + 25 * p["cluster"] + 15 * p["fresh"] + 20 * segment_fit))


# ---------- бюджет подробной оценки ----------

def eval_allowance(used_today, now):
    """Сколько подробных оценок можно сделать к этому часу (часть бюджета — на поздние часы)."""
    h = local_hour(now)
    allowed = min(C.DAILY_EVAL_BUDGET,
                  C.EVAL_BUDGET_START + math.floor((C.DAILY_EVAL_BUDGET - C.EVAL_BUDGET_START) * h / 24))
    return max(0, allowed - used_today)


def pick_for_eval(cands, prelim, n):
    """Топ по предварительной оценке; часть мест — свежим клипам."""
    if n <= 0 or not cands:
        return []
    ranked = sorted(cands, key=lambda c: (-prelim[c]["prelim"], c))
    fresh = [c for c in ranked if prelim[c]["age_h"] * 60 <= C.FRESH_MINUTES]
    k_fresh = min(len(fresh), max(1, int(round(n * C.FRESH_SHARE)))) if fresh else 0
    chosen = fresh[:k_fresh]
    for c in ranked:
        if len(chosen) >= n:
            break
        if c not in chosen:
            chosen.append(c)
    return chosen[:n]


# ---------- дневная подборка ----------

def _pick_diverse(pool, k, used_types):
    """По убыванию балла; при близких баллах (±3) предпочитаем ещё не взятый тип события."""
    pool = list(pool)
    out = []
    while pool and len(out) < k:
        best = pool[0]
        if best["moment_type"] in used_types:
            for alt in pool[1:]:
                if best["rank"] - alt["rank"] > C.DIVERSITY_WINDOW:
                    break
                if alt["moment_type"] not in used_types:
                    best = alt
                    break
        pool.remove(best)
        out.append(best)
        used_types.add(best["moment_type"])
    return out


def decide_day(day_clips, finalize):
    """Решения по клипам одних суток.

    day_clips: список {id, status, score, score_mode, moment_type}. Учитываются только
    представители групп (дубли отсеяны раньше). Возвращает dict id -> новый статус
    ('render_queued' | 'reserve' | 'rejected') только для изменившихся клипов.
    """
    active = [c for c in day_clips if c["status"] in RENDER_ACTIVE]
    used_types = {c.get("moment_type") or "other" for c in active}
    n_active = len(active)
    open_ = [c for c in day_clips if c["status"] in ("scored", "reserve") and c.get("score") is not None]
    for c in open_:
        c.setdefault("moment_type", "other")
        # шкалы Claude и метаданных не смешиваем: Claude-оценённые идут первыми
        c["rank"] = c["score"] + (1000 if c["score_mode"] == "claude" else 0)
    open_.sort(key=lambda c: (-c["rank"], c["id"]))
    out = {}

    def claude(c, lo, hi=101):
        return c["score_mode"] == "claude" and lo <= c["score"] < hi

    def meta(c, lo):
        return c["score_mode"] == "meta" and c["score"] >= lo

    if not finalize:
        slots = max(0, (C.TARGET_PER_DAY - C.LATE_SLOTS) - n_active)
        pool = [c for c in open_ if claude(c, C.SCORE_IMMEDIATE) or meta(c, C.META_IMMEDIATE)]
        for c in _pick_diverse(pool, slots, used_types):
            out[c["id"]] = "render_queued"
        for c in open_:
            if c["id"] in out:
                continue
            if c["score_mode"] == "claude" and c["score"] < C.SCORE_RESERVE:
                out[c["id"]] = "rejected"
            elif c["status"] != "reserve":
                out[c["id"]] = "reserve"
        return out

    # после эфира (или ближе к концу суток во время марафона): добор до цели из резерва
    need = max(0, C.TARGET_PER_DAY - n_active)
    pool = [c for c in open_ if claude(c, C.SCORE_RESERVE) or meta(c, C.META_FLOOR)]
    picked = _pick_diverse(pool, need, used_types)
    for c in picked:
        out[c["id"]] = "render_queued"
    total = n_active + len(picked)
    # седьмой — только отдельный сильный момент 85+ по оценке Claude
    if total == C.TARGET_PER_DAY:
        extra = [c for c in pool if c not in picked and claude(c, C.SCORE_IMMEDIATE)]
        if extra:
            out[extra[0]["id"]] = "render_queued"
    for c in open_:
        if c["id"] in out:
            continue
        if c["score_mode"] == "claude" and c["score"] < C.SCORE_RESERVE:
            out[c["id"]] = "rejected"
        elif c["status"] != "reserve":
            out[c["id"]] = "reserve"
    return out


def should_finalize(stream, now):
    """Эфир завершён (офлайн ≥30 мин) или марафон и уже поздно по Еревану."""
    live = stream.get("live", False)
    if live:
        return local_hour(now) >= C.MARATHON_SUMMARY_HOUR
    last = stream.get("last_live_at")
    if not last:
        return True
    return (now - parse_ts(last)) >= timedelta(minutes=C.STREAM_END_GRACE_MIN)


def fit_window(start, end, dur, lo=C.CLIP_MIN_SEC, hi=C.CLIP_MAX_SEC):
    """Окно монтажа 30–40 с внутри клипа; короче 30 с — весь клип.

    Удлиняем в обе стороны (начало — понять, что происходит; конец — развязка),
    длинное окно режем с начала, сохраняя финал с реакцией.
    """
    start, end = max(0.0, float(start)), min(float(dur), float(end))
    if end <= start:
        start, end = 0.0, float(dur)
    if dur <= lo:
        return 0.0, float(dur)
    length = end - start
    if length < lo:
        grow = lo - length
        s_room, e_room = start, dur - end
        add_s = min(s_room, grow * 0.6)
        add_e = min(e_room, grow - add_s)
        add_s = min(s_room, grow - add_e)
        start, end = start - add_s, end + add_e
    elif length > hi:
        start = end - hi
    return round(start, 3), round(end, 3)
