#!/usr/bin/env python3
"""Прототип нарезчика стримов GTA 6 -> вертикальный клип 1080x1920.

Шаги:
  1. Анализ исходника через FFmpeg: громкость звука (RMS по секундам, astats)
     и динамика картинки (scene score по кадрам, scdet).
  2. Выбор одного цельного окна 30–40 с с максимальной «активностью»;
     границы сдвигаются к тихим секундам, чтобы не резать фразу/событие.
  3. Рендер 1080x1920: сверху вебка стримера, ниже геймплей (центр. кроп),
     поверх геймплея — увеличенный таймер, внизу — субтитры (если есть).
  4. Проверка результата через ffprobe.

Регионы вебки и таймера задаются в координатах исходника (--cam, --timer,
формат x:y:w:h). По умолчанию — раскладка тестового исходника.
Субтитры: берётся SRT рядом с исходником (<имя>.srt) или --srt; если
установлен openai-whisper и SRT нет — делается расшифровка.
"""
import argparse
from functools import lru_cache
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path]).stdout
    return json.loads(out)


def audio_rms_per_sec(src, dur, start=0):
    """RMS (дБ) по окнам 1 с через astats + ametadata."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
         "-t", f"{dur:.3f}", "-i", src, "-vn", "-ac", "1",
         "-af", "asetnsamples=n=48000:p=0,aresample=48000,"
                "astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"], text=True, capture_output=True, check=True)
    vals = []
    for line in r.stdout.splitlines():
        if "RMS_level=" in line:
            v = line.split("=", 1)[1]
            vals.append(-120.0 if v in ("-inf", "inf", "nan") else float(v))
    n = int(dur)
    arr = np.full(n, -120.0)
    arr[:min(n, len(vals))] = vals[:n]
    return arr


def motion_per_sec(src, dur, start=0):
    """Средний scene score кадров за каждую секунду (анализ на 320px)."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
         "-t", f"{dur:.3f}", "-i", src, "-an",
         "-vf", "scale=320:-2,scdet=threshold=0,"
                "metadata=print:key=lavfi.scd.score:file=-",
         "-f", "null", "-"], text=True, capture_output=True, check=True)
    n = int(dur)
    sums, cnt = np.zeros(n), np.zeros(n)
    t = None
    for line in r.stdout.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        elif "lavfi.scd.score=" in line and t is not None:
            i = int(t)
            if i < n:
                sums[i] += float(line.split("=", 1)[1])
                cnt[i] += 1
    return np.divide(sums, np.maximum(cnt, 1))


def norm(x):
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    return np.clip((x - lo) / (hi - lo + 1e-9), 0, 1)


def pick_window(rms, motion, min_len=30, max_len=40, w_audio=0.6):
    score = w_audio * norm(rms) + (1 - w_audio) * norm(motion)
    n = len(score)
    if n <= min_len:
        return 0, n, score
    cs = np.concatenate([[0], np.cumsum(score)])
    best = None
    for L in range(min_len, min(max_len, n) + 1):
        sums = cs[L:] - cs[:-L]
        # средняя активность, небольшой бонус за длину
        vals = sums / L + 0.002 * L
        i = int(np.argmax(vals))
        if best is None or vals[i] > best[0]:
            best = (vals[i], i, i + L)
    _, s, e = best
    # «Цельность»: начинаем чуть раньше подъёма (2 с контекста) и
    # заканчиваем на спаде, не выходя за 30–40 с.
    quiet = score < np.percentile(score, 40)
    for _ in range(3):
        if s > 0 and e - (s - 1) <= max_len and not quiet[s - 1]:
            s -= 1
    s = max(0, s - 2) if e - max(0, s - 2) <= max_len else s
    while e < n and e - s < max_len and not quiet[e - 1]:
        e += 1
    return s, e, score


def fmt_tc(t):
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def shift_srt(srt_in, srt_out, start, end):
    """Вырезает субтитры окна [start, end) и сдвигает к нулю."""
    def p(ts):
        h, m, rest = ts.strip().split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def f(t):
        t = max(0.0, t)
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = re.split(r"\n\s*\n", open(srt_in, encoding="utf-8").read().strip())
    out, k = [], 0
    for b in blocks:
        lines = b.strip().splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, z = (p(x) for x in lines[1].split("-->"))
        if z <= start or a >= end:
            continue
        k += 1
        out.append(f"{k}\n{f(max(a, start) - start)} --> {f(min(z, end) - start)}\n"
                   + "\n".join(lines[2:]))
    open(srt_out, "w", encoding="utf-8").write("\n\n".join(out) + "\n")
    return k


@lru_cache(maxsize=2)
def load_whisper_model(name):
    import whisper

    return whisper.load_model(name)


def transcribe_window(src, start, end):
    with tempfile.TemporaryDirectory(prefix="narezchik-") as temp_dir:
        audio = os.path.join(temp_dir, "selected-audio.wav")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio],
            check=True,
        )
        model = load_whisper_model(os.environ.get("WHISPER_MODEL", "small"))
        result = model.transcribe(audio, language="ru")
    return [
        {
            "start": float(segment["start"]) + start,
            "end": float(segment["end"]) + start,
            "text": segment["text"].strip(),
        }
        for segment in result["segments"]
    ]


def write_srt(segments, srt_out):
    with open(srt_out, "w", encoding="utf-8") as fh:
        for i, segment in enumerate(segments, 1):
            def f(t):
                ms = int(t * 1000)
                return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"

            fh.write(
                f"{i}\n{f(segment['start'])} --> {f(segment['end'])}\n"
                f"{segment['text']}\n\n"
            )


def try_whisper(src, srt_out, start, end):
    try:
        segments = transcribe_window(src, start, end)
    except ImportError:
        return False
    write_srt(segments, srt_out)
    return True


def parse_box(s):
    x, y, w, h = (int(v) for v in s.split(":"))
    return x, y, w, h


def render(src, out, start, end, cam, timer, srt, src_w, src_h, layout, variant):
    W, H = 1080, 1920
    CAM_H = 608                      # вебка 16:9 на всю ширину
    GAME_H = H - CAM_H               # 1312
    if layout == "full":
        if variant == "classic":
            fx = (f"[0:v]split=2[bg][fg];"
                  f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},boxblur=24:12,eq=brightness=-0.3[bg2];"
                  f"[fg]scale={W}:-2:force_original_aspect_ratio=decrease[fg2];"
                  "[bg2][fg2]overlay=(W-w)/2:(H-h)/2[v0]")
            caption_style = (
                "FontName=DejaVu Sans,Bold=1,FontSize=15,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
                "Alignment=2,MarginV=70"
            )
            audio_filter = "loudnorm=I=-14:TP=-1.5:LRA=11"
        elif variant == "framed":
            fx = (f"[0:v]split=2[bg][fg];"
                  f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},boxblur=32:16,eq=brightness=-0.18:saturation=1.2[bg2];"
                  f"[fg]scale={W - 96}:-2:force_original_aspect_ratio=decrease[fg2];"
                  "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,"
                  f"drawbox=x=42:y=650:w={W - 84}:h=620:color=0x42E8C4:t=8[v0]")
            caption_style = (
                "FontName=DejaVu Sans,Bold=1,FontSize=17,PrimaryColour=&H0000FFFF,"
                "OutlineColour=&H00101010,BorderStyle=3,BackColour=&H99000000,"
                "Outline=2,Shadow=0,Alignment=2,MarginV=100"
            )
            audio_filter = "loudnorm=I=-12:TP=-1.0:LRA=9"
        else:
            fx = (f"[0:v]split=2[bg][fg];"
                  f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},boxblur=18:9,eq=brightness=-0.4:saturation=0.85[bg2];"
                  f"[fg]scale={W}:-2:force_original_aspect_ratio=decrease[fg2];"
                  "[bg2][fg2]overlay=(W-w)/2:(H-h)/2,"
                  f"drawbox=x=0:y=330:w={W}:h=18:color=0xFF654F:t=fill,"
                  f"drawbox=x=0:y=1570:w={W}:h=18:color=0xFF654F:t=fill[v0]")
            caption_style = (
                "FontName=DejaVu Sans,Bold=1,FontSize=14,PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00FF654F,BorderStyle=1,Outline=4,Shadow=0,"
                "Alignment=2,MarginV=130"
            )
            audio_filter = "loudnorm=I=-16:TP=-2.0:LRA=13"
        fc = fx
    else:
        caption_style = (
            "FontName=DejaVu Sans,Bold=1,FontSize=13,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=60"
        )
        audio_filter = "loudnorm=I=-14:TP=-1.5:LRA=11"
        # Central gameplay crop with the webcam stacked above it.
        gw = int(round(src_h * W / GAME_H)) // 2 * 2
        gx = (src_w - gw) // 2
        cx, cy, cw, ch = cam
        tx, ty, tw, th = timer
        t_scale = 1.6
        tw_o, th_o = int(tw * t_scale) // 2 * 2, int(th * t_scale) // 2 * 2
        fc = (
            f"[0:v]split=3[a][b][c];"
            f"[a]crop={gw}:{src_h}:{gx}:0,scale={W}:{GAME_H}:flags=lanczos,setsar=1[game];"
            f"[b]crop={cw}:{ch}:{cx}:{cy},scale={W}:{CAM_H}:flags=lanczos,setsar=1[cam];"
            f"[c]crop={tw}:{th}:{tx}:{ty},scale={tw_o}:{th_o}:flags=lanczos[tmr];"
            f"[cam][game]vstack=inputs=2[st];"
            f"[st]drawbox=x=0:y={CAM_H - 3}:w={W}:h=6:color=white@0.9:t=fill[st2];"
            f"[st2][tmr]overlay=x={W - tw_o - 30}:y={CAM_H + 30}[v0]"
        )
    clip_duration = end - start
    fc += (
        ";[v0]drawbox=x=52:y=108:w=510:h=72:color=0x080D17@0.78:t=fill,"
        "drawbox=x=52:y=108:w=12:h=72:color=0x42E8C4:t=fill,"
        "drawtext=font='Sans':text='GTA VI / HIGHLIGHT':x=82:y=125:"
        "fontsize=34:fontcolor=white,"
        f"drawbox=x=56:y=1820:w=968:h=8:color=white@0.35:t=fill,"
        f"drawbox=x=56:y=1820:w='968*t/{clip_duration:.3f}':h=8:"
        "color=0x42E8C4:t=fill[vgraphic]"
    )
    last = "vgraphic"
    if srt:
        esc = srt.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        fc += f";[{last}]subtitles='{esc}':original_size={W}x{H}:force_style='{caption_style}'[v1]"
        last = "v1"

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src,
           "-filter_complex", fc, "-map", f"[{last}]", "-map", "0:a:0",
           "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "8M", "-bufsize", "16M",
           "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-af", audio_filter, out]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="output/clip.mp4")
    ap.add_argument("--cam", default="1416:786:480:270", help="x:y:w:h вебки в исходнике")
    ap.add_argument("--timer", default="20:20:380:80", help="x:y:w:h таймера в исходнике")
    ap.add_argument("--srt", help="SRT с таймкодами исходника")
    ap.add_argument("--min", type=int, default=30)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--layout", choices=("stacked", "full"), default="stacked",
                    help="stacked: вебка + геймплей; full: исходное видео на размытой подложке")
    ap.add_argument("--variant", choices=("classic", "framed", "cinematic"), default="classic")
    ap.add_argument("--duration", type=int, choices=(30, 35, 40),
                    help="длительность монтажной версии; момент активности остаётся по центру")
    ap.add_argument("--start-sec", type=float,
                    help="начало заданного фрагмента в секундах исходного видео")
    ap.add_argument("--end-sec", type=float,
                    help="конец заданного фрагмента в секундах исходного видео")
    ap.add_argument("--reuse-report", help="переиспользовать выбор момента из JSON другого рендера")
    ap.add_argument("--no-whisper", action="store_true",
                    help="не запускать Whisper, если SRT отсутствует")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} не найден")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)

    info = probe(a.src)
    dur = float(info["format"]["duration"])
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    src_w, src_h = int(vs["width"]), int(vs["height"])
    print(f"[1/4] Исходник: {a.src} {src_w}x{src_h}, {dur:.2f} с")

    if (a.start_sec is None) != (a.end_sec is None):
        ap.error("--start-sec and --end-sec must be used together")
    if a.start_sec is not None and a.reuse_report:
        ap.error("--reuse-report cannot be combined with an explicit time range")

    if a.start_sec is not None:
        if a.start_sec < 0 or a.end_sec <= a.start_sec or a.end_sec > dur:
            ap.error("the explicit time range must be within the source video")
        center = (a.start_sec + a.end_sec) / 2
        rms = mot = score = np.array([], dtype=float)
        print(f"[1/4] Использую найденный момент: "
              f"{fmt_tc(a.start_sec)} — {fmt_tc(a.end_sec)}")
    elif a.reuse_report:
        with open(a.reuse_report, encoding="utf-8") as fh:
            previous = json.load(fh)
        if os.path.abspath(previous["source"]) != os.path.abspath(a.src):
            ap.error("--reuse-report points to a different source video")
        rms = np.asarray(previous["per_second"]["rms_db"])
        mot = np.asarray(previous["per_second"]["motion"])
        score = np.asarray(previous["per_second"]["score"])
        center = (previous["source_start_sec"] + previous["source_end_sec"]) / 2
        print("[1/4] Использую анализ момента из предыдущего рендера")
    else:
        rms = audio_rms_per_sec(a.src, dur)
        mot = motion_per_sec(a.src, dur)
        s, e, score = pick_window(rms, mot, a.min, a.max)
        center = (s + e) / 2
        print(f"[1/4] Найден активный фрагмент: {fmt_tc(s)} — {fmt_tc(e)}")

    duration = min(float(a.duration or min(a.max, max(a.min, dur))), dur)
    start = max(0.0, center - duration / 2)
    end = min(dur, start + duration)
    start = max(0.0, end - duration)
    if score.size:
        s, e = int(start), min(int(np.ceil(end)), len(score))
        activity = (
            f", ср. активность {score[s:e].mean():.2f} "
            f"vs {score.mean():.2f} по всему видео"
        )
    else:
        activity = ""
    print(f"[2/4] Выбран момент: {fmt_tc(start)} — {fmt_tc(end)} "
          f"({end - start:.1f} с){activity}")

    base = os.path.splitext(a.out)[0]
    srt_src = a.srt or (os.path.splitext(a.src)[0] + ".srt")
    if not os.path.exists(srt_src) and a.reuse_report:
        previous_srt = os.path.splitext(a.reuse_report)[0] + ".full.srt"
        if os.path.exists(previous_srt):
            srt_src = previous_srt
    clip_srt = None
    if not os.path.exists(srt_src) and not a.no_whisper:
        srt_src = base + ".full.srt"
        if not try_whisper(a.src, srt_src, start, end):
            srt_src = None
    elif a.no_whisper and not os.path.exists(srt_src):
        srt_src = None
    if srt_src:
        clip_srt = base + ".srt"
        k = shift_srt(srt_src, clip_srt, start, end)
        print(f"[3/4] Субтитры: {k} реплик из {srt_src}")
        if k == 0:
            clip_srt = None
    else:
        if a.no_whisper:
            print("[3/4] Субтитры: распознавание отключено — без субтитров")
        else:
            print("[3/4] Субтитры: расшифровки нет (whisper не установлен, SRT не найден) — без субтитров")

    render(a.src, a.out, start, end, parse_box(a.cam), parse_box(a.timer),
           clip_srt, src_w, src_h, a.layout, a.variant)

    o = probe(a.out)
    ov = next(x for x in o["streams"] if x["codec_type"] == "video")
    oa = [x for x in o["streams"] if x["codec_type"] == "audio"]
    od = float(o["format"]["duration"])
    report = {
        "source": a.src,
        "source_start": fmt_tc(start), "source_end": fmt_tc(end),
        "source_start_sec": start, "source_end_sec": end,
        "output": a.out, "duration_sec": round(od, 3),
        "variant": a.variant,
        "resolution": f"{ov['width']}x{ov['height']}",
        "video_codec": ov["codec_name"],
        "audio": [{"codec": x["codec_name"], "sample_rate": x["sample_rate"],
                   "channels": x["channels"]} for x in oa],
        "subtitles": bool(clip_srt),
        "per_second": {"rms_db": [round(v, 1) for v in rms.tolist()],
                       "motion": [round(v, 2) for v in mot.tolist()],
                       "score": [round(v, 3) for v in score.tolist()]},
    }
    json.dump(report, open(base + ".json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    expected_min = min(a.min, dur)
    expected_max = min(a.max, dur)
    ok = ((ov["width"], ov["height"]) == (1080, 1920) and oa
          and expected_min <= od <= expected_max + 0.5)
    print(f"[4/4] ffprobe: {ov['width']}x{ov['height']} {ov['codec_name']}, "
          f"{od:.2f} с, аудио: {len(oa)} дорожк. -> {'OK' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
