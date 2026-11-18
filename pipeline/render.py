"""Монтаж одного момента существующим рендером (narezchik.render_tiktok) + проверка ffprobe."""
import os

from . import config as C
from . import transcribe as T


def render_clip(src, rec, out_dir, transcript):
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
    if transcript and transcript.get("segments"):
        cue_list = T.cues(transcript, start, end)
        if cue_list:
            srt = T.write_srt(cue_list, base + ".srt")
        else:
            notes.append("в выбранном отрезке нет распознанной речи — без субтитров")
    elif rec.get("whisper_error"):
        notes.append(f"Whisper: {rec['whisper_error']} — MP4 без субтитров")

    cam = tuple(rec["cam"]) if rec.get("cam") else None
    if cam is None:
        cam, _ = cam_detect.detect(src)
    title_png = None
    if rec.get("title"):
        title_png = base + "_title.png"
        title_card.render_title(rec["title"], rec.get("title_accent", ""), title_png)
    mp4 = base + ".mp4"
    if cam:
        face = cam_detect.faces_center_in_box(src, cam)
        zoom = None
        if rec.get("zoom_at") is not None:
            t = rec["zoom_at"] - start
            if 0.5 <= t <= (end - start) - 1.0:
                zoom = (max(0.0, t - 0.6), min(end - start, t + 1.6))
                notes.append(f"вебка увеличена на реакции: {zoom[0]:.1f}–{zoom[1]:.1f} с ролика")
        narezchik.render_tiktok(src, mp4, start, end, cam, title_png, w, h, face, srt=srt, zoom=zoom)
    else:
        notes.append("вебка не найдена — компоновка «весь кадр + крупный план»")
        narezchik.render_full(src, mp4, start, end, None, srt, w, h)

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
    }
    ok = (ov["width"], ov["height"]) == (1080, 1920) and bool(oa) and abs(od - want) <= 0.6
    if want < C.CLIP_MIN_SEC - 0.5:
        notes.append(f"исходный клип короче 30 с ({want:.1f} с) — взят целиком")
    meta = write_publish_files(base, rec)
    return {"ok": ok, "mp4": mp4, "srt": srt, "checks": checks, "notes": notes, "meta": meta}


def publish_texts(rec):
    """Заголовок и описания для TikTok / YouTube Shorts / Reels по профилю канала."""
    p = C.PROFILES.get(rec.get("channel") or "strogo", C.PROFILES["strogo"])
    title = (rec.get("title") or "").strip() or (rec.get("title_fallback") or "").strip()
    if not title:
        title = f"{p['name'].upper()} В GTA 6"
    s, e = rec["window"]
    src_line = f"Источник: {rec['url']} (фрагмент {s:.1f}–{e:.1f} с клипа)"
    tags = p.get("tiktok") or f"twitch: {p['login']}"
    shorts_title = title if not p.get("shorts_suffix") else f"{title} | {p['shorts_suffix']}"
    out = {
        "title": title,
        "tiktok": f"{title}\n{tags}",
        "shorts_title": shorts_title[:100],
        "shorts_description": f"{title}\n{p.get('shorts_suffix') or tags}\n{src_line}",
        "reels": f"{title}\n{p.get('reels') or tags}",
        "source": rec["url"],
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
