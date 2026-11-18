"""Поиск обнажённых участков (голая грудь и т. п.) для размытия — NudeNet (модель в пакете).

Кадры отрезка анализируются ~4 раза в секунду: целиком и четвертями (модель работает на 320 px,
четверти помогают с мелкими фигурами в кадре 1440p). Найденные рамки объединяются в треки
по времени и расширяются с запасом — их размывает рендер (narezchik.finish, параметр blur).
"""
import subprocess

import numpy as np

BLUR_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
}
MIN_SCORE = 0.35

_DET = None


def _detector():
    global _DET
    if _DET is None:
        from nudenet import NudeDetector
        _DET = NudeDetector()
    return _DET


def _frames(src, start, end, fps, src_w, src_h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                          "-i", src, "-vf", f"fps={fps}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    size = src_w * src_h * 3
    for i in range(len(raw) // size):
        yield i / fps, np.frombuffer(raw[i * size:(i + 1) * size], np.uint8).reshape(src_h, src_w, 3)


def detect_regions(src, start, end, src_w, src_h, fps=4, max_regions=12):
    """[{t0, t1, x, y, w, h}] во времени готового ролика (0 = start) и координатах исходника."""
    det = _detector()
    hits = []  # (t, x, y, w, h)
    hw, hh = src_w // 2, src_h // 2
    tiles = [(0, 0, src_w, src_h)] + [(x, y, hw, hh) for y in (0, hh) for x in (0, hw)]
    for t, frame in _frames(src, start, end, fps, src_w, src_h):
        for tx, ty, tw, th in tiles:
            img = frame[ty:ty + th, tx:tx + tw]
            for d in det.detect(np.ascontiguousarray(img)):
                if d.get("class") in BLUR_CLASSES and d.get("score", 0) >= MIN_SCORE:
                    x, y, w, h = d["box"]
                    hits.append((t, tx + x, ty + y, w, h))
    # треки: близкие по времени (≤0.75 с) и пересекающиеся рамки
    hits.sort()
    tracks = []
    for t, x, y, w, h in hits:
        for tr in tracks:
            ox = min(tr["x"] + tr["w"], x + w) - max(tr["x"], x)
            oy = min(tr["y"] + tr["h"], y + h) - max(tr["y"], y)
            if t - tr["t1"] <= 0.75 and ox > -0.5 * w and oy > -0.5 * h:
                x0, y0 = min(tr["x"], x), min(tr["y"], y)
                x1, y1 = max(tr["x"] + tr["w"], x + w), max(tr["y"] + tr["h"], y + h)
                tr.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0, t1=t)
                break
        else:
            tracks.append({"t0": t, "t1": t, "x": x, "y": y, "w": w, "h": h})
    out = []
    for tr in sorted(tracks, key=lambda r: -(r["t1"] - r["t0"]))[:max_regions]:
        pad_w, pad_h = tr["w"] * 0.35 + 12, tr["h"] * 0.35 + 12
        x = max(0, int(tr["x"] - pad_w))
        y = max(0, int(tr["y"] - pad_h))
        w = min(src_w - x, int(tr["w"] + 2 * pad_w)) // 2 * 2
        h = min(src_h - y, int(tr["h"] + 2 * pad_h)) // 2 * 2
        if w >= 8 and h >= 8:
            out.append({"t0": round(max(0.0, tr["t0"] - 0.4), 2), "t1": round(tr["t1"] + 0.6, 2),
                        "x": x, "y": y, "w": w, "h": h})
    return out
