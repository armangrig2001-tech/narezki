"""Распознавание речи faster-whisper: одна расшифровка с таймкодами — и для оценки, и для субтитров."""
import os

from . import config as C

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        os.makedirs(C.WHISPER_CACHE, exist_ok=True)
        _MODEL = WhisperModel(C.WHISPER_MODEL, device="cpu", compute_type=C.WHISPER_COMPUTE,
                              download_root=C.WHISPER_CACHE)
    return _MODEL


def transcribe(path):
    """{language, probability, segments:[{start,end,text,words:[{start,end,word}]}], text}."""
    segments, info = _model().transcribe(path, language=None, word_timestamps=True,
                                         vad_filter=True, beam_size=5)
    segs = []
    for s in segments:
        words = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word}
                 for w in (s.words or [])]
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                     "words": words})
    return {"language": info.language, "probability": round(info.language_probability, 3),
            "segments": segs, "text": " ".join(s["text"] for s in segs).strip()}


def transcript_for_prompt(tr, limit=4000):
    if not tr or not tr.get("segments"):
        return "(речь не распознана)"
    lines = [f"[{s['start']:.1f}–{s['end']:.1f}] {s['text']}" for s in tr["segments"]]
    out = "\n".join(lines)
    return out[:limit]


def cues(tr, start, end, max_chars=34, max_dur=2.8):
    """Короткие реплики для вертикали из слов с таймкодами, сдвинутые под обрезку [start, end)."""
    out = []
    cur, c0, c1 = [], None, None
    words = [w for s in (tr or {}).get("segments", []) for w in s.get("words", [])]
    if not words:  # нет пословных таймкодов — сегменты целиком
        for s in (tr or {}).get("segments", []):
            if s["end"] > start and s["start"] < end:
                out.append((max(s["start"], start) - start, min(s["end"], end) - start, s["text"]))
        return out

    def flush():
        if cur:
            out.append((c0, c1, " ".join(cur).strip()))

    for w in words:
        if w["end"] <= start or w["start"] >= end:
            continue
        ws, we = max(w["start"], start) - start, min(w["end"], end) - start
        token = w["word"].strip()
        if not token:
            continue
        text = (" ".join(cur + [token])).strip()
        if cur and (len(text) > max_chars or we - c0 > max_dur or ws - c1 > 0.8):
            flush()
            cur, c0 = [], None
        if c0 is None:
            c0 = ws
        cur.append(token)
        c1 = we
    flush()
    return [(a, max(b, a + 0.4), t) for a, b, t in out if t]


def write_srt(cue_list, path):
    def f(t):
        ms = int(round(max(0.0, t) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    with open(path, "w", encoding="utf-8") as fh:
        for i, (a, b, t) in enumerate(cue_list, 1):
            fh.write(f"{i}\n{f(a)} --> {f(b)}\n{t}\n\n")
    return path
