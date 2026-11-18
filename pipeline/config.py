"""Параметры конвейера нарезок (канал, сутки, бюджеты, пороги)."""
import os

CHANNEL = os.environ.get("PIPELINE_CHANNEL", "strogo")
CATEGORY = os.environ.get("PIPELINE_CATEGORY", "Grand Theft Auto VI")
TZ = "Asia/Yerevan"

DISCOVERY_HOURS = 48          # окно поиска клипов в Helix
DAILY_EVAL_BUDGET = 24        # подробных оценок в сутки
EVAL_BUDGET_START = 6         # доступно с начала суток; остальное открывается по часам
FRESH_MINUTES = 90            # «свежий» клип
FRESH_SHARE = 0.34            # доля мест в выборке на оценку для свежих клипов

TARGET_PER_DAY = 6            # цель: 6 разных событий
MIN_PER_DAY = 5
MAX_PER_DAY = 7
LATE_SLOTS = 2                # до конца эфира держим 2 места для поздних моментов
SCORE_IMMEDIATE = 85          # оценка Claude: сразу в монтаж
SCORE_RESERVE = 75            # оценка Claude: резерв
META_IMMEDIATE = 70           # отбор по метаданным (внутренняя шкала, не пороги Claude)
META_FLOOR = 40
DIVERSITY_WINDOW = 3          # «близкие оценки» — в пределах 3 баллов

STREAM_END_GRACE_MIN = 30     # эфир считается завершённым через 30 мин офлайна
MARATHON_SUMMARY_HOUR = 23    # во время марафона подводим итог после 23:00 по Еревану

PROXIMITY_SECONDS = 120       # близкое время создания — повод сравнить клипы
VOD_TOLERANCE = 5.0           # допуск пересечения таймкодов VOD, с

MAX_EVAL_PER_RUN = int(os.environ.get("PIPELINE_MAX_EVAL", "4"))
MAX_RENDER_PER_RUN = int(os.environ.get("PIPELINE_MAX_RENDER", "3"))
MAX_ATTEMPTS = 3              # повтор при временной ошибке

CLIP_MIN_SEC = 30.0
CLIP_MAX_SEC = 40.0

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_CACHE = os.environ.get("WHISPER_CACHE", os.path.expanduser("~/.cache/faster-whisper"))

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

# Баллы из 100
WEIGHTS = {"strength": 25, "clarity": 25, "reaction": 20, "hook": 15, "views": 10, "vertical": 5}
