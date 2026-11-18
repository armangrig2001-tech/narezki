"""Состояние конвейера между запусками (JSON в ветке pipeline-state).

Структура:
  clips[clip_id] = {url, created_at, title, video_id, vod_offset, duration, views,
                    views_history: [[iso, views], ...], status, score, score_mode, ...}
  same_event: [[id_a, id_b], ...]      — подтверждённые пары «один момент»
  checked_pairs: ["a|b", ...]          — пары, которые уже сравнивали
  days[YYYY-MM-DD] = {evaluated, rendered: [...], finalized}
  stream = {live, last_live_at, checked_at}

Статусы клипа:
  new → (duplicate | eval_retry | eval_failed | scored)
  scored → (render_queued | reserve | rejected)
  render_queued → (rendered | render_retry | render_failed)
"""
import json
import os

PROCESSED = {"rendered", "render_queued", "render_retry", "duplicate", "rejected",
             "render_failed", "eval_failed", "source_unavailable"}
OPEN = {"new", "eval_retry", "source_retry"}   # ждут подробной оценки (в т.ч. повтор загрузки)
RENDER_ACTIVE = {"rendered", "render_queued", "render_retry"}


def empty_state(channel):
    return {"version": 1, "channel": channel, "clips": {}, "same_event": [],
            "checked_pairs": [], "days": {}, "stream": {}, "runs": []}


def load(path, channel):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        for k, v in empty_state(channel).items():
            st.setdefault(k, v)
        return st
    return empty_state(channel)


def save(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def day(state, key):
    return state["days"].setdefault(key, {"evaluated": 0, "rendered": [], "finalized": False})
