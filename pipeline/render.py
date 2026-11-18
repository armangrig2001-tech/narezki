"""Монтаж одного момента существующим рендером (narezchik.render_tiktok) + проверка ffprobe."""
import os

from . import config as C
from . import transcribe as T


MAT = ("хуй", "хуе", "хуё", "пизд", "ебл", "ебат", "ебан", "ёб", "еба", "бля", "сук", "нахуй", "мудак", "пидор",
       "залуп", "шлюх", "гандон", "дроч")
FILLER = {"НУ", "КОРОЧЕ", "ТИПА", "ВОТ", "ЭЭ", "ЭМ", "ААА", "ЭТО", "ЗНАЧИТ", "СЛУШАЙ", "БЛИН", "ДА", "НЕТ",
          "ОКЕЙ", "ОК", "ТАК", "ВООБЩЕ", "ПРОСТО", "ЧЕ", "ЧЁ"}


def phrase_from_transcript(tr, window, max_words=5):
    """Бесплатная фраза заголовка из расшифровки Whisper: короткая яркая реплика без мата и «воды»."""
    import re
    if not tr or not tr.get("segments"):
        return ""
    s0, s1 = window
    best = None
    for seg in tr["segments"]:
        if seg["end"] < s0 or seg["start"] > s1:
            continue
        text = seg["text"].strip()
        low = text.lower()
        if any(m in low for m in MAT):
            continue
        words = [w for w in re.findall(r"[\w-]+[!?]?", text, flags=re.U)
                 if w.upper().strip("!?") not in FILLER and not re.fullmatch(r"\d+", w)]
        if len(words) < 2 or not any(len(w.strip("!?")) >= 4 for w in words):
            continue
        score = -abs(len(words) - 4)                       # 3–5 слов — лучше всего
        score += 1.5 if ("!" in text or "?" in text) else 0  # восклицание/вопрос — эмоция
        score += 1.0 * (seg["start"] - s0) / max(1.0, s1 - s0)  # ближе к развязке/реакции
        if best is None or score > best[0]:
            best = (score, words[:max_words])
    return " ".join(best[1]).upper().rstrip(",.:") if best else ""


def build_title(rec):
    """Заголовок: ник стримера + о чём момент, например «ZLOYn В СТРИПКЛУБЕ ГТА 6».

    Фраза: от Claude (если есть ключ) → осмысленное название клипа на Twitch → реплика стримера из
    расшифровки Whisper (бесплатно) → просто «В ГТА 6». Без упоминания игры добавляется «ГТА 6».
    """
    import re
    p = C.PROFILES.get(rec.get("channel") or "strogo", C.PROFILES["strogo"])
    name = p.get("title_name") or p["login"].upper()
    drop = {name.upper(), p["login"].upper(), p["name"].upper(), "СТРОГО", "ЭВЕЛОН", "ЗЛОЙ"}

    def clean(phrase):
        words = [w for w in re.split(r"\s+", (phrase or "").strip()) if w and w.upper().strip("!?.,:") not in drop]
        return words[:5]

    def meaningful(ws):
        core = [re.sub(r"[^\w]", "", w, flags=re.U) for w in ws]
        return any(len(c) >= 4 for c in core) or sum(len(c) >= 3 for c in core) >= 2

    words = clean(rec.get("title"))                         # Claude
    if not meaningful(words):
        words = clean(rec.get("title_fallback"))            # название клипа (без мусора)
    if not meaningful(words) and rec.get("window"):
        words = clean(phrase_from_transcript(rec.get("transcript"), rec["window"]))  # реплика (Whisper)
    text = " ".join(words).upper()
    if not re.search(r"GTA|ГТА", text):
        text = (text + " ГТА 6").strip() if words else "В ГТА 6"
    return f"{name} {text}".strip(), name


def render_clip(src, rec, out_dir, transcript=None):
    import cam_detect
    import narezchik
    import title_card

    os.makedirs(out_dir, exist_ok=True)
    info = narezchik.probe(src)
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(vs["width"]), int(vs["height"])
    start, end = rec["window"]
    base = os.path.join(out_dir, rec["slug"])
    notes = []

    srt = None
    if C.BURN_SUBTITLES and transcript and transcript.get("segments"):
        cue_list = T.cues(transcript, start, end)
        srt = T.write_srt(cue_list, base + ".srt") if cue_list else None

    blur = []
    if C.BLUR_NUDITY:
        try:
            from . import nsfw
            blur = nsfw.detect_regions(src, start, end, w, h)
            if blur:
                notes.append(f"размыто обнажённых участков: {len(blur)} "
                             f"(" + ", ".join(f"{r['t0']:.1f}–{r['t1']:.1f} с" for r in blur[:4]) + ")")
        except Exception as e:  # детектор недоступен — сообщаем, ролик не выпускаем без проверки
            raise RuntimeError(f"проверка на обнажённость не выполнена: {type(e).__name__}: {e}") from None

    cam = tuple(rec["cam"]) if rec.get("cam") else None
    if cam is None:
        cam, _ = cam_detect.detect(src)
    title, name = build_title(rec)
    rec["final_title"] = title
    title_png = base + "_title.png"
    title_card.render_title(title, name, title_png, keep=(name,))
    mp4 = base + ".mp4"
    if cam:
        face = cam_detect.faces_center_in_box(src, cam)
        zoom = None
        if rec.get("zoom_at") is not None:
            t = rec["zoom_at"] - start
            if 0.5 <= t <= (end - start) - 1.0:
                zoom = (max(0.0, t - 0.6), min(end - start, t + 1.6))
                notes.append(f"вебка увеличена на реакции: {zoom[0]:.1f}–{zoom[1]:.1f} с ролика")
        narezchik.render_tiktok(src, mp4, start, end, cam, title_png, w, h, face, srt=srt, zoom=zoom, blur=blur)
    else:
        notes.append("вебка не найдена — компоновка «весь кадр + крупный план»")
        narezchik.render_full(src, mp4, start, end, None, srt, w, h, title_png=title_png, blur=blur)

    o = narezchik.probe(mp4)
    ov = next(s for s in o["streams"] if s["codec_type"] == "video")
    oa = [s for s in o["streams"] if s["codec_type"] == "audio"]
    od = float(o["format"]["duration"])
    want = end - start
    checks = {
        "resolution": f"{ov['width']}x{ov['height']}",
        "duration": round(od, 2),
        "audio": bool(oa),
        "size_mb": round(int(o["format"]["size"]) / 1e6, 1),
        "blurred_regions": len(blur),
    }
    ok = (ov["width"], ov["height"]) == (1080, 1920) and bool(oa) and abs(od - want) <= 0.6
    if want < C.CLIP_MIN_SEC - 0.5:
        notes.append(f"исходный клип короче 30 с ({want:.1f} с) — взят целиком")
    meta = write_publish_files(base, rec)
    return {"ok": ok, "mp4": mp4, "srt": srt, "checks": checks, "notes": notes, "meta": meta}


def publish_texts(rec):
    """Заголовок и описания для TikTok / YouTube Shorts / Reels по профилю канала."""
    p = C.PROFILES.get(rec.get("channel") or "strogo", C.PROFILES["strogo"])
    title = rec.get("final_title") or build_title(rec)[0]
    s, e = rec["window"]
    srcinfo = rec.get("source") or {}
    if srcinfo.get("kind") in ("vod", "vod_context") and rec.get("video_id"):
        t = int(srcinfo["start"] + s)
        link = f"https://www.twitch.tv/videos/{rec['video_id']}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"
        src_line = f"Источник: {link}" + (f" (клип: {rec['url']})" if rec.get("kind") != "vod" else "")
    else:
        src_line = f"Источник: {rec['url']} (фрагмент {s:.1f}–{e:.1f} с клипа)"
    tags = p.get("tiktok") or f"twitch: {p['login']}"
    shorts_title = title if not p.get("shorts_suffix") else f"{title} | {p['shorts_suffix']}"
    out = {
        "title": title,
        "tiktok": f"{title}\n{tags}",
        "shorts_title": shorts_title[:100],
        "shorts_description": f"{title}\n{p.get('shorts_suffix') or tags}\n{src_line}",
        "reels": f"{title}\n{p.get('reels') or tags}",
        "source": src_line.replace("Источник: ", ""),
        "upload_note": p.get("upload_note") or "",
        "contest_ok": rec.get("contest_ok"),
    }
    return out


def write_publish_files(base, rec):
    import json
    t = publish_texts(rec)
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(t, fh, ensure_ascii=False, indent=1)
    lines = [f"Заголовок: {t['title']}", "", "TikTok (описание):", t["tiktok"], "",
             f"YouTube Shorts (заголовок): {t['shorts_title']}", "YouTube Shorts (описание):",
             t["shorts_description"], "", "Instagram Reels (описание):", t["reels"], "",
             f"Исходный клип: {t['source']}"]
    if t["upload_note"]:
        lines += ["", f"Инструкция к выгрузке: {t['upload_note']}"]
    with open(base + ".txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return t
