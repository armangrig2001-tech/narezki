"""Медиа: загрузка исходника (yt-dlp, как в Render Twitch Clip), кадры, сравнение клипов по звуку."""
import json
import os
import re
import subprocess

import numpy as np


class MediaError(Exception):
    pass


def download(url, out_dir):
    """Публичный клип Twitch через yt-dlp без логина и cookies (рабочий способ из Render Twitch Clip)."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "source.mp4")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    r = subprocess.run(["yt-dlp", "--no-cookies", "--no-playlist", "--no-progress",
                        "-f", "best[ext=mp4]/best", "--merge-output-format", "mp4",
                        "-o", os.path.join(out_dir, "source.%(ext)s"), "--", url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(path):
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise MediaError("yt-dlp: " + " | ".join(tail))
    return path


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(path):
    return float(probe(path)["format"]["duration"])


def scene_changes(path, threshold=0.3, limit=6):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
                        f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    ts = [float(m) for m in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    # самые разнесённые по времени
    out = []
    for t in ts:
        if all(abs(t - o) > 2.0 for o in out):
            out.append(t)
    return out[:limit]


def extract_frames(path, out_dir, dur, width=768):
    """Кадры: начало, конец и вокруг заметных изменений сцены (до 10 штук)."""
    os.makedirs(out_dir, exist_ok=True)
    times = [min(0.5, dur / 4), dur * 0.25, dur * 0.5, dur * 0.75, max(0.0, dur - 0.6)]
    for t in scene_changes(path):
        times += [max(0.0, t - 0.7), min(dur - 0.1, t + 0.7)]
    uniq = []
    for t in sorted(times):
        if all(abs(t - u) > 1.0 for u in uniq):
            uniq.append(round(t, 2))
    frames = []
    for t in uniq[:10]:
        fp = os.path.join(out_dir, f"frame_{t:06.2f}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "4", fp], check=False)
        if os.path.exists(fp):
            frames.append((t, fp))
    return frames


def contact_sheet(frames, out_path, w=480, h=270):
    """Лист кадров (до 8) для отчёта."""
    frames = frames[:8]
    if not frames:
        return None
    n, cols = len(frames), min(4, len(frames))
    inputs = []
    for _, fp in frames:
        inputs += ["-i", fp]
    fc = "".join(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2[s{i}];" for i in range(n))
    if n == 1:
        fc += "[s0]null"
    else:
        pos = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(n))
        fc += "".join(f"[s{i}]" for i in range(n)) + f"xstack=inputs={n}:layout={pos}:fill=black"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", fc, "-frames:v", "1",
                    out_path], check=False)
    return out_path if os.path.exists(out_path) else None


# ---------- сравнение двух клипов одного момента ----------

def audio_envelope(path, rate=20):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    hop = 8000 // rate
    n = len(a) // hop
    if n < rate * 3:
        return None
    env = np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)
    env = np.log(env)
    return (env - env.mean()) / (env.std() + 1e-6)


def audio_similarity(path_a, path_b, rate=20, min_overlap_s=5.0):
    """Максимальная нормированная корреляция огибающих громкости при сдвиге (общий звук = один момент)."""
    a, b = audio_envelope(path_a, rate), audio_envelope(path_b, rate)
    if a is None or b is None:
        return 0.0
    best = 0.0
    min_ov = int(min_overlap_s * rate)
    for lag in range(-len(b) + min_ov, len(a) - min_ov + 1):
        a0, b0 = max(0, lag), max(0, -lag)
        n = min(len(a) - a0, len(b) - b0)
        if n < min_ov:
            continue
        x, y = a[a0:a0 + n], b[b0:b0 + n]
        x = x - x.mean()
        y = y - y.mean()
        d = np.sqrt((x * x).sum() * (y * y).sum())
        if d > 0:
            best = max(best, float((x * y).sum() / d))
    return best


def text_similarity(ta, tb):
    wa = {w for w in re.findall(r"\w{3,}", (ta or "").lower())}
    wb = {w for w in re.findall(r"\w{3,}", (tb or "").lower())}
    if len(wa) < 4 or len(wb) < 4:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def segment_fit(dur, transcript_text, rms_db=None):
    """Пригодность отрезка для ролика 30–40 с (для отбора по метаданным), 0..1."""
    f = 1.0 if dur >= 30 else max(0.2, dur / 30)
    if transcript_text is not None and len(transcript_text.split()) < 4:
        f *= 0.8  # почти без речи
    return round(f, 3)
