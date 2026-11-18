"""Записи стримов (VOD): длинные ролики вокруг клипов и поиск моментов по записи — в том числе
во время эфира (запись текущей трансляции растёт, каждый запуск разбирает её новый кусок).

Поток для поиска моментов:
  1. Helix GET /videos?type=archive — последние записи канала (идущий эфир тоже есть в списке).
  2. Из записи скачивается только звук нового куска (yt-dlp --download-sections, формат audio only).
  3. Whisper + сигналы по секундам: всплески речи, эмоциональные слова и громкость относительно
     привычного уровня стримера (сама по себе громкость не решает), места, где зрители делали клипы.
  4. Лучшие пики (≈35 с, не ближе 90 с друг к другу) становятся кандидатами-«моментами записи»
     и идут в общий отбор, проверку повторов и монтаж; видео скачивается только для них.
"""
import os
import re
import subprocess
from datetime import timedelta

import numpy as np

from . import config as C
from .media import MediaError

CHUNK_SEC = 20 * 60          # кусок записи за один запуск
MIN_CHUNK_SEC = 8 * 60       # пока эфир идёт — ждать, пока наберётся хотя бы столько
LIVE_EDGE_SEC = 90           # самые свежие секунды идущей записи не трогаем
BACKFILL_SEC = 90 * 60       # при первом знакомстве с записью — не глубже 1,5 ч назад
MOMENT_LEN = 35
MIN_GAP_SEC = 90
MAX_MOMENTS_PER_CHUNK = 3

EXCITED = ("вау", "ахах", "хаха", "ахаха", "офиге", "охрене", "ебать", "ебат", "бля", "пиздец", "нифига",
           "капец", "жесть", "лол", "ору", "господи", "боже", "аааа", "нееет", "стоп", "смотри", "что?",
           "ничего себе", "красава", "имба", "легенда", "ахуе", "охуе", "ебан", "сука", "бляха", "ура")


def vod_url(video_id):
    return f"https://www.twitch.tv/videos/{video_id}"


def parse_duration(s):
    """'3h2m10s' → секунды."""
    total = 0
    for n, u in re.findall(r"(\d+)([hms])", s or ""):
        total += int(n) * {"h": 3600, "m": 60, "s": 1}[u]
    return total


def list_vods(tw, user_id, first=5):
    data = tw.get("videos", {"user_id": user_id, "type": "archive", "first": first}).get("data", [])
    return [{"id": v["id"], "created_at": v["created_at"], "duration": parse_duration(v.get("duration")),
             "title": v.get("title", ""), "viewable": v.get("viewable", "public")} for v in data]


def download_section(video_id, start, end, out_path, audio_only=False):
    """Отрезок записи [start, end] с. Начало может сместиться на ≤2 с назад (ключевой кадр)."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    base = os.path.splitext(out_path)[0]
    fmt = "bestaudio/worst" if audio_only else "best[height<=1440]/best"
    try:
        r = subprocess.run(["yt-dlp", "--no-cookies", "--no-playlist", "--no-progress", "-f", fmt,
                            "--download-sections", f"*{max(0, int(start))}-{int(end)}",
                            "-o", base + ".%(ext)s", "--", vod_url(video_id)],
                           capture_output=True, text=True, timeout=1200)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"yt-dlp (запись) не запустился: {type(e).__name__}: {e}") from None
    got = [f for f in os.listdir(os.path.dirname(out_path) or ".")
           if f.startswith(os.path.basename(base) + ".") and not f.endswith(".part")]
    if r.returncode != 0 or not got:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise MediaError("запись недоступна: " + " | ".join(tail))
    src = os.path.join(os.path.dirname(out_path) or ".", got[0])
    if src != out_path:
        os.replace(src, out_path)
    return out_path


def _rms_per_sec(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    n = len(a) // 8000
    if n == 0:
        return np.zeros(0)
    return 20 * np.log10(np.sqrt((a[: n * 8000].reshape(n, 8000) ** 2).mean(axis=1)) + 1e-6)


def _z(x):
    if x.size == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) + 1e-6
    return np.clip((x - med) / (1.4826 * mad), -3, 6)


def find_moments(audio_path, transcript, clip_offsets=(), base=0.0):
    """Пики интереса в куске записи. Возвращает [{start, score, reasons}] (секунды записи)."""
    rms = _rms_per_sec(audio_path)
    n = len(rms)
    if n < MOMENT_LEN:
        return []
    words = np.zeros(n)
    excited = np.zeros(n)
    for seg in (transcript or {}).get("segments", []):
        low = seg["text"].lower()
        k = sum(low.count(w) for w in EXCITED) + low.count("!") + low.count("?") * 0.5
        for w in seg.get("words") or []:
            t = int(w["start"])
            if 0 <= t < n:
                words[t] += 1
        t0, t1 = int(seg["start"]), min(n - 1, int(seg["end"]))
        if k and t1 >= t0:
            excited[t0:t1 + 1] += k / max(1, t1 - t0 + 1)
    clips = np.zeros(n)
    for off in clip_offsets:
        t = int(off - base)
        if 0 <= t < n:
            clips[max(0, t - 5):min(n, t + 25)] += 1
    k5 = np.ones(5) / 5
    loud = np.convolve(_z(rms), k5, "same")
    rate = np.convolve(_z(words), k5, "same")
    exc = np.convolve(np.clip(excited, 0, 3), np.ones(7) / 7, "same")
    # громкость лишь подтверждает речь и эмоции; сильнее всего — клипы зрителей
    score = 0.25 * np.clip(loud, 0, None) + 0.3 * np.clip(rate, 0, None) + 0.45 * exc * 2 + 1.2 * np.clip(clips, 0, 3)
    win = np.convolve(score, np.ones(MOMENT_LEN) / MOMENT_LEN, "same")
    thr = max(np.percentile(win, 90), np.median(win) + 0.4)
    out = []
    for i in np.argsort(-win):
        if win[i] < thr or len(out) >= MAX_MOMENTS_PER_CHUNK:
            break
        if any(abs(i - o["_peak"]) < MIN_GAP_SEC for o in out):
            continue
        start = int(max(0, min(n - MOMENT_LEN, i - MOMENT_LEN * 0.55)))
        sl = slice(start, start + MOMENT_LEN)
        reasons = []
        if clips[sl].max() > 0:
            reasons.append("здесь зрители делали клипы")
        if exc[sl].max() > 0.3:
            reasons.append("эмоциональные реплики")
        if rate[sl].max() > 1.5:
            reasons.append("всплеск речи")
        if loud[sl].max() > 2:
            reasons.append("громкая реакция")
        out.append({"start": base + start, "score": float(win[i]), "_peak": int(i),
                    "reasons": reasons or ["пик активности"]})
    for o in out:
        o.pop("_peak", None)
    return out


def pending_chunk(v, live, state_vod):
    """Следующий кусок записи для анализа или None."""
    done = state_vod.get("analyzed_until")
    if done is None:
        done = max(0, v["duration"] - BACKFILL_SEC)
    edge = v["duration"] - (LIVE_EDGE_SEC if live else 0)
    if edge - done < (MIN_CHUNK_SEC if live else 60):
        return None
    return done, min(edge, done + CHUNK_SEC)


def moment_record(channel, v, m):
    from .select import iso, parse_ts
    start = int(m["start"])
    return {
        "id": f"vod-{v['id']}-{start}", "url": vod_url(v["id"]), "kind": "vod", "channel": channel,
        "created_at": iso(parse_ts(v["created_at"]) + timedelta(seconds=start)),
        "title": "", "video_id": v["id"], "vod_offset": start, "duration": float(MOMENT_LEN),
        "views": 0, "views_history": [], "vod_score": round(m["score"], 3),
        "vod_reasons": m["reasons"], "status": "new", "attempts": 0, "slug": f"vod-{v['id']}-{start}",
    }


def context_window(off, dur, vod_len=None, pre=None, post=None):
    """Отрезок записи вокруг клипа: подводка + момент + реакция, всего ≈30–40 с."""
    want = max(C.CLIP_MIN_SEC, min(C.CLIP_MAX_SEC, dur + 20))
    extra = max(0.0, want - dur)
    pre = min(off, extra * 0.55) if pre is None else pre
    post = extra - pre if post is None else post
    s, e = off - pre, off + dur + post
    if vod_len:
        e = min(e, vod_len)
    return max(0.0, s), e
