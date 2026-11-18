"""Оценка момента Claude по кадрам и расшифровке (только при заданном ANTHROPIC_API_KEY).

Возвращает структурированный результат: тип момента, баллы по критериям, причину выбора,
start_sec / end_sec и заголовок. Динамику просмотров (10 баллов) считает конвейер, не Claude.
"""
import base64
import json
import os

from . import config as C
from .transcribe import transcript_for_prompt

MOMENT_TYPES = ["crash", "fail", "funny_npc", "unexpected_mechanic", "streamer_reaction",
                "fight", "chase", "police", "stunt", "glitch", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "moment_type": {"type": "string", "enum": MOMENT_TYPES},
        "strength": {"type": "integer", "description": "сила момента, 0–25"},
        "clarity": {"type": "integer", "description": "понятность без контекста, 0–25"},
        "reaction": {"type": "integer", "description": "реакция стримера или развязка, 0–20"},
        "hook": {"type": "integer", "description": "первые 2–3 секунды выбранного отрезка, 0–15"},
        "vertical": {"type": "integer", "description": "пригодность для вертикали 9:16, 0–5"},
        "reason": {"type": "string", "description": "почему этот момент, 1–2 предложения по-русски"},
        "start_sec": {"type": "number"},
        "end_sec": {"type": "number"},
        "title": {"type": "string", "description": "заголовок 3–7 слов ЗАГЛАВНЫМИ, интрига без спойлера"},
        "title_accent": {"type": "string", "description": "одно главное слово заголовка"},
    },
    "required": ["moment_type", "strength", "clarity", "reaction", "hook", "vertical", "reason",
                 "start_sec", "end_sec", "title", "title_accent"],
    "additionalProperties": False,
}

SYSTEM = """Ты монтажёр вертикальных нарезок (TikTok / Shorts / Reels) из стримов по GTA 6.
Тебе дают кадры одного клипа Twitch с отметками времени и расшифровку речи с таймкодами.
Оценивай сами кадры и речь. Название клипа — только подсказка, не доказательство.

Оцени момент по критериям (целые числа):
- strength 0–25: сила момента (краш, фейл, смешной NPC, неожиданная механика, сильная реакция);
- clarity 0–25: понятно ли зрителю без контекста стрима;
- reaction 0–20: есть ли реакция стримера или развязка;
- hook 0–15: цепляют ли первые 2–3 секунды выбранного отрезка;
- vertical 0–5: читается ли в вертикали (вебка сверху, игра снизу).
Будь строг: 20+ по силе — только редкие яркие моменты.

Выбери отрезок start_sec / end_sec внутри клипа длиной 30–40 с (если клип короче — весь клип):
в начале зритель должен успеть понять, что происходит (без долгой подводки), в конце — увидеть
развязку или реакцию; закончи сразу после пика реакции.
Заголовок: 3–7 слов, ЗАГЛАВНЫМИ, интрига без спойлера, без эмодзи, кавычек и точки в конце;
title_accent — одно главное слово из заголовка."""


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _img(fp):
    with open(fp, "rb") as fh:
        return base64.standard_b64encode(fh.read()).decode("ascii")


def score(frames, transcript, meta):
    """frames: [(t, path)], transcript: dict|None, meta: {title, duration, views, created_at}."""
    import anthropic

    content = []
    for t, fp in frames:
        content.append({"type": "text", "text": f"Кадр t={t:.1f} с"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": _img(fp)}})
    content.append({"type": "text", "text": (
        f"Длительность клипа: {meta['duration']:.1f} с. Название (подсказка): {meta.get('title') or '—'}.\n"
        f"Язык речи: {(transcript or {}).get('language', 'неизвестен')}.\n"
        f"Расшифровка:\n{transcript_for_prompt(transcript)}\n\nВерни оценку по схеме.")})
    req = dict(model=C.CLAUDE_MODEL, max_tokens=16000, system=SYSTEM,
               messages=[{"role": "user", "content": content}],
               output_config={"effort": "medium",
                              "format": {"type": "json_schema", "schema": SCHEMA}})
    client = anthropic.Anthropic()
    try:
        # серверный запасной вариант на случай отказа классификатора
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                           fallbacks="default", **req)
    except (anthropic.BadRequestError, TypeError):
        resp = client.messages.create(**req)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude отказался оценивать клип")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    lim = {"strength": 25, "clarity": 25, "reaction": 20, "hook": 15, "vertical": 5}
    for k, mx in lim.items():
        data[k] = max(0, min(mx, int(data.get(k, 0))))
    return data
