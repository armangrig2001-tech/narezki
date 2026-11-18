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
        narezchik.render_tiktok(src, mp4, start, end, cam, title_png, w, h, face, srt=srt)
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
    return {"ok": ok, "mp4": mp4, "srt": srt, "checks": checks, "notes": notes}
