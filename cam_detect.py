#!/usr/bin/env python3
"""Автоматический поиск вебки стримера в кадре стрима.

Идея: вебка — это прямоугольник, который стоит на месте весь клип, а внутри
него лицо, которое тоже почти не двигается по экрану. Игровые персонажи
двигаются вместе с камерой игры, поэтому их «лица» по кадрам разбросаны.

Шаги:
  1. Берём ~48 кадров по всей длине клипа (ffmpeg -> серые кадры 960 px).
  2. Ищем лица (Haar: анфас + профиль, зеркальный профиль).
  3. Кластеризуем центры лиц; вебка = самый частый кластер, который почти
     не смещается (разброс < 4 % ширины кадра) и встречается в >= 25 % кадров.
  4. Границы рамки вебки ищем от лица наружу: на каждой стороне берём
     самую сильную «постоянную» прямую (среднее по кадрам |градиента|)
     плюс перепад временной изменчивости (за рамкой игра меняется сильно,
     внутри вебки фон статичен). Нет выраженной линии — край кадра или
     запас от лица.
  5. Нет лица — запасной путь: ищем постоянную прямоугольную рамку в углах.

Результат: x:y:w:h в пикселях исходника + оценка уверенности.
"""
import json
import subprocess
import sys

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

AW = 960  # ширина кадров анализа


def sample_frames(src, n=48):
    pr = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", src],
        check=True, capture_output=True, text=True).stdout)
    w, h = pr["streams"][0]["width"], pr["streams"][0]["height"]
    dur = float(pr["format"]["duration"])
    ah = int(round(h * AW / w / 2)) * 2
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", src, "-vf",
         f"fps={n}/{dur:.3f},scale={AW}:{ah},format=gray",
         "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8)
    k = fr.size // (AW * ah)
    return fr[: k * AW * ah].reshape(k, ah, AW), (w, h)


def detect_faces(frames):
    base = cv2.data.haarcascades
    cascades = [cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml"),
                cv2.CascadeClassifier(base + "haarcascade_profileface.xml")]
    out = []  # (frame_idx, x, y, w, h)
    for i, f in enumerate(frames):
        g = cv2.equalizeHist(f)
        for j, c in enumerate(cascades):
            for img, flip in ((g, False), (cv2.flip(g, 1), True)) if j == 1 else ((g, False),):
                for (x, y, w, h) in c.detectMultiScale(img, 1.1, 6, minSize=(22, 22)):
                    if flip:
                        x = img.shape[1] - x - w
                    out.append((i, x, y, w, h))
    return out


def stable_face(faces, n_frames):
    if not faces:
        return None, {}
    pts = np.array([(x + w / 2, y + h / 2, w, h, i) for i, x, y, w, h in faces], float)
    best = None
    rad = 0.04 * AW
    for cx, cy, *_ in pts:
        m = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) < rad
        frames_hit = len(set(pts[m, 4].astype(int)))
        if best is None or frames_hit > best[0]:
            best = (frames_hit, m)
    hits, m = best
    sel = pts[m]
    info = {"face_detections": len(faces), "stable_frames": hits, "frames": n_frames,
            "spread_px": float(np.hypot(sel[:, 0].std(), sel[:, 1].std()))}
    if hits < max(3, 0.25 * n_frames):
        return None, info
    med = np.median(sel[:, :4], axis=0)
    return med, info  # cx, cy, w, h (в координатах анализа)


def edge_maps(frames):
    f = frames.astype(np.float32)
    gx = np.abs(np.diff(f, axis=2)).mean(axis=0)          # вертикальные линии
    gy = np.abs(np.diff(f, axis=1)).mean(axis=0)          # горизонтальные линии
    tstd = f.std(axis=0)                                  # изменчивость во времени
    return gx, gy, tstd


def _best_line(profile_edge, step, lo, hi, edge_value):
    """Индекс сильнейшей линии в [lo, hi) или edge_value, если явной линии нет."""
    lo, hi = max(lo, 1), min(hi, len(profile_edge) - 1)
    if hi - lo < 3:
        return edge_value, 0.0
    seg = profile_edge[lo:hi] + step[lo:hi]
    i = int(np.argmax(seg))
    strength = float(seg[i] / (np.median(seg) + 1e-6))
    if strength < 2.2:
        return edge_value, strength
    return lo + i, strength


def box_from_face(face, gx, gy, tstd):
    cx, cy, fw, fh = face
    H, W = tstd.shape
    y0, y1 = int(max(0, cy - 1.5 * fh)), int(min(H, cy + 1.5 * fh))
    x0, x1 = int(max(0, cx - 1.5 * fw)), int(min(W, cx + 1.5 * fw))
    # профили по столбцам/строкам в полосе вокруг лица
    col_e = gx[y0:y1].mean(axis=0)
    row_e = gy[:, x0:x1].mean(axis=1)
    col_t = tstd[y0:y1].mean(axis=0)
    row_t = tstd[:, x0:x1].mean(axis=1)

    def step(p, k=6):
        c = np.convolve(p, np.ones(k) / k, mode="same")
        s = np.zeros_like(p)
        s[k:-k] = np.abs(c[2 * k:] - c[:-2 * k])
        return s / (s.max() + 1e-6) * (np.median(p) * 4 + 1e-6)

    sc, sr = step(col_t), step(row_t)
    col_e = np.append(col_e, col_e[-1])
    row_e = np.append(row_e, row_e[-1])
    near, far = 0.9, 6.0
    L, sL = _best_line(col_e, sc, int(cx - far * fw), int(cx - near * fw), 0)
    R, sR = _best_line(col_e, sc, int(cx + near * fw), int(cx + far * fw), W)
    T, sT = _best_line(row_e, sr, int(cy - far * fh), int(cy - near * fh), 0)
    B, sB = _best_line(row_e, sr, int(cy + near * fh), int(cy + far * fh), H)
    # нет линии и край кадра далеко — разумный запас от лица
    if L == 0 and cx - 3 * fw > 0 and sL < 2.2:
        L = int(max(0, cx - 3 * fw))
    if R == W and cx + 3 * fw < W and sR < 2.2:
        R = int(min(W, cx + 3 * fw))
    if T == 0 and cy - 2.5 * fh > 0 and sT < 2.2:
        T = int(max(0, cy - 2.5 * fh))
    if B == H and cy + 2.5 * fh < H and sB < 2.2:
        B = int(min(H, cy + 2.5 * fh))
    return (L, T, R - L, B - T), {"line_strength": [round(v, 2) for v in (sL, sR, sT, sB)]}


def box_from_corners(gx, gy, tstd):
    """Без лица: постоянная прямоугольная рамка в одном из углов."""
    H, W = tstd.shape
    best = None
    for corner in ("tl", "tr", "bl", "br"):
        xs = slice(0, W // 2) if corner[1] == "l" else slice(W // 2, W)
        ys = slice(0, H // 2) if corner[0] == "t" else slice(H // 2, H)
        ce = gx[ys, xs].mean(axis=0)
        re = gy[ys, xs].mean(axis=1)
        ci, ri = int(np.argmax(ce[2:-2])) + 2, int(np.argmax(re[2:-2])) + 2
        s = ce[ci] / (np.median(ce) + 1e-6) + re[ri] / (np.median(re) + 1e-6)
        x = ci + xs.start
        y = ri + ys.start
        if corner == "tl":
            box = (0, 0, x, y)
        elif corner == "tr":
            box = (x, 0, W - x, y)
        elif corner == "bl":
            box = (0, y, x, H - y)
        else:
            box = (x, y, W - x, H - y)
        if box[2] < W * 0.12 or box[3] < H * 0.12:
            continue
        if best is None or s > best[0]:
            best = (s, box, corner)
    if best is None or best[0] < 8:
        return None, {"corner_score": round(float(best[0]), 2) if best else 0}
    return best[1], {"corner": best[2], "corner_score": round(best[0], 2)}


def detect(src):
    if cv2 is None:
        return None, {"error": "opencv не установлен"}
    frames, (sw, sh) = sample_frames(src)
    gx, gy, tstd = edge_maps(frames)
    faces = detect_faces(frames)
    face, info = stable_face(faces, len(frames))
    if face is not None:
        box, extra = box_from_face(face, gx, gy, tstd)
        info.update(extra, method="face")
        info["face_box_src"] = [int(v * sw / AW) for v in
                                (face[0] - face[2] / 2, face[1] - face[3] / 2, face[2], face[3])]
    else:
        box, extra = box_from_corners(gx, gy, tstd)
        info.update(extra, method="corner")
        if box is None:
            info["method"] = "none"
            return None, info
    k = sw / AW
    x, y, w, h = (int(round(v * k)) for v in box)
    x, y = max(0, x), max(0, y)
    w, h = min(sw - x, w) // 2 * 2, min(sh - y, h) // 2 * 2
    if w < sw * 0.08 or h < sh * 0.08:
        info["method"] += "-too-small"
        return None, info
    return (x, y, w, h), info


def faces_center_in_box(src, box, n=24):
    """Центр (x, y) лиц внутри найденной вебки, в пикселях исходника; None, если лиц нет."""
    if cv2 is None:
        return None
    x, y, w, h = box
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "json", src], capture_output=True, text=True).stdout)
    dur = float(pr["format"]["duration"])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vf",
                          f"fps={n}/{dur:.3f},crop={w}:{h}:{x}:{y},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    k = len(raw) // (w * h)
    if not k:
        return None
    fr = np.frombuffer(raw[: k * w * h], np.uint8).reshape(k, h, w)
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    pts = []
    for f in fr:
        for (fx, fy, fw, fh) in casc.detectMultiScale(cv2.equalizeHist(f), 1.08, 5,
                                                      minSize=(max(24, h // 10),) * 2):
            pts.append((fx, fy, fw, fh))
    if not pts:
        return None
    a = np.array(pts, float)
    # все устойчивые лица (кластеры по X): берём охват от самого левого до самого правого
    left = np.percentile(a[:, 0], 10)
    right = np.percentile(a[:, 0] + a[:, 2], 90)
    cy = np.median(a[:, 1] + a[:, 3] / 2)
    return float(x + (left + right) / 2), float(y + cy), float(right - left)


def save_debug(src, box, info, path):
    """Кадр из середины клипа с рамкой найденной вебки (для проверки)."""
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "json", src], capture_output=True, text=True).stdout)
    t = float(pr["format"]["duration"]) / 2
    vf = "scale=1280:-2"
    if box:
        x, y, w, h = box
        fb = info.get("face_box_src")
        vf = f"drawbox=x={x}:y={y}:w={w}:h={h}:color=lime:t=8"
        if fb:
            vf += f",drawbox=x={fb[0]}:y={fb[1]}:w={fb[2]}:h={fb[3]}:color=red:t=4"
        vf += ",scale=1280:-2"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", src,
                    "-frames:v", "1", "-vf", vf, path], check=False)


if __name__ == "__main__":
    b, i = detect(sys.argv[1])
    print(json.dumps({"cam": b, **i}, ensure_ascii=False, default=float))
    if len(sys.argv) > 2:
        save_debug(sys.argv[1], b, i, sys.argv[2])
