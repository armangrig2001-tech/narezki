#!/usr/bin/env python3
"""Заголовок ролика PNG: заглавные, акцентное слово жёлтым, тёмная плашка.

Правила (вертикальные нарезки):
  * 3–7 слов, максимум 2 строки, ЗАГЛАВНЫМИ, без эмодзи и точки в конце;
  * шрифт Druk Super, запасной Montserrat Black (assets/fonts/);
  * 80–100 px при ширине 1080, строка не шире ~960 px, иначе меньше кегль (не < 64);
  * белый текст на тёмной плашке, главное слово — жёлтое.
"""
import glob
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
YELLOW = (255, 212, 0, 255)
WHITE = (255, 255, 255, 255)
PLATE = (0, 0, 0, 200)


def find_font():
    fonts = os.path.join(HERE, "assets", "fonts")
    for pat in ("*[Dd]ruk*[Ss]uper*.[ot]tf", "*[Dd]ruk*.[ot]tf", "Montserrat-Black.ttf", "*Black*.[ot]tf"):
        hit = sorted(glob.glob(os.path.join(fonts, pat)))
        if hit:
            return hit[0]
    raise FileNotFoundError("нет шрифта в assets/fonts (Druk Super или Montserrat-Black.ttf)")


def split_lines(words, font, max_w, draw):
    """Одна строка, если влезает, иначе лучший перенос на две."""
    full = " ".join(words)
    if draw.textlength(full, font=font) <= max_w or len(words) == 1:
        return [words]
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        w = max(draw.textlength(a, font=font), draw.textlength(b, font=font))
        if best is None or w < best[0]:
            best = (w, [words[:i], words[i:]])
    return best[1]


def render_title(text, accent, out, max_w=960, size=90, min_size=64, pad=(30, 16), radius=18):
    text = text.strip().rstrip(".").upper()
    accent_set = {w.strip().upper() for w in (accent or "").split(",") if w.strip()}
    words = text.split()
    font_path = find_font()
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    while True:
        font = ImageFont.truetype(font_path, size)
        lines = split_lines(words, font, max_w - 2 * pad[0], probe)
        widest = max(probe.textlength(" ".join(l), font=font) for l in lines)
        if (widest <= max_w - 2 * pad[0] and len(lines) <= 2) or size <= min_size:
            break
        size -= 2
    asc, desc = font.getmetrics()
    line_h = int((asc + desc) * 0.98)
    space = probe.textlength(" ", font=font)
    W = int(widest + 2 * pad[0])
    H = int(line_h * len(lines) + 2 * pad[1])
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=PLATE)
    y = pad[1]
    for line in lines:
        lw = probe.textlength(" ".join(line), font=font)
        x = (W - lw) / 2
        for w in line:
            col = YELLOW if w.strip("«»\"',!?:;").upper() in accent_set else WHITE
            d.text((x, y), w, font=font, fill=col)
            x += probe.textlength(w, font=font) + space
        y += line_h
    img.save(out)
    return {"font": os.path.basename(font_path), "size": size, "lines": [" ".join(l) for l in lines],
            "png": out, "w": W, "h": H}


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(render_title(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "",
                                  sys.argv[3] if len(sys.argv) > 3 else "title.png"),
                     ensure_ascii=False))
