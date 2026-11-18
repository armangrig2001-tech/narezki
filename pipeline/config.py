"""Параметры конвейера нарезок (канал, сутки, бюджеты, пороги)."""
import os

CHANNEL = os.environ.get("PIPELINE_CHANNEL", "strogo")
CATEGORY = os.environ.get("PIPELINE_CATEGORY", "Grand Theft Auto VI")
TZ = "Asia/Yerevan"

DISCOVERY_HOURS = 48          # окно поиска клипов в Helix
DAILY_EVAL_BUDGET = 24        # подробных оценок в сутки
EVAL_BUDGET_START = 6         # доступно с начала суток; остальное открывается по часам
FRESH_MINUTES = 90            # «свежий» клип
FRESH_SHARE = 0.2             # доля мест в выборке на оценку для свежих клипов (главное — популярность)

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

MIN_CLIP_SEC = 5.0            # клипы короче 5 с не берём
VOD_CONTEXT = True            # короткий клип → 30–40 с из записи стрима вокруг него
VOD_SCAN = True               # поиск моментов по записи стрима (и во время эфира)
CLIP_MIN_SEC = 30.0
CLIP_MAX_SEC = 40.0

BURN_SUBTITLES = False        # субтитры на ролик не накладываются (расшифровка — только для отбора)
BLUR_NUDITY = True            # размывать обнажённую грудь и т. п. (NudeNet)

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
WHISPER_CACHE = os.environ.get("WHISPER_CACHE", os.path.expanduser("~/.cache/faster-whisper"))

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

# Баллы из 100
WEIGHTS = {"strength": 25, "clarity": 25, "reaction": 20, "hook": 15, "views": 10, "vertical": 5}


# ---------- профили каналов ----------
# Каждый канал: свой файл состояния в ветке pipeline-state, свой дневной лимит (Asia/Yerevan),
# свои правила отбора и монтажа. Строго — прежние настройки без изменений.
PROFILES = {
    "strogo": {
        "login": "strogo", "name": "Строго", "state_file": "state.json", "title_name": "STROGO",
        "discovery_hours": 48, "max_per_day": 7, "contest_timer": True,
        "cam_zoom": False, "no_cam": "full",           # без вебки — «весь кадр + крупный план»
        "claude_notes": "",
        "tiktok": "", "shorts_suffix": "", "reels": "", "upload_note": "",
    },
    "evelone2004": {
        "login": "evelone2004", "name": "Эвелон", "state_file": "evelone2004.json", "title_name": "EVELONE",
        "discovery_hours": 24, "max_per_day": 6, "contest_timer": False,
        "cam_zoom": True, "no_cam": "full_if_lead",    # полный кадр — только если роль Эвелона ясна
        "claude_notes": (
            "Стример — Эвелон (evelone2004). Он должен быть главным участником момента: в сценах "
            "с Мокривским, Shadowkek и другими выбирай эпизоды, где именно реплика, решение или "
            "реакция Эвелона ведёт сцену. streamer_leads_scene = true, только если это так."),
        "tiktok": "twitch: evelone2004 #evelone", "shorts_suffix": "twitch: evelone2004 #evelone",
        "reels": "twitch: evelone2004 #evelone", "upload_note": "",
    },
    "zloyn": {
        "login": "zloyn", "name": "Злой", "state_file": "zloyn.json", "title_name": "ZLOYn",
        "discovery_hours": 24, "max_per_day": 6, "contest_timer": False,
        "cam_zoom": False, "no_cam": "full_if_lead",
        "claude_notes": (
            "Стример — Злой (Артур, ZLOYn). Выбирай эпизоды, где Артур в центре действия; если клип "
            "держится на его реакции, в кадре должен быть геймплей, который объясняет, на что он "
            "реагирует. excluded_content = true для реакций на фильмы, сериалы, аниме и любого "
            "отвлекающего фонового видеоряда."),
        "tiktok": "twitch: ZLOYn https://www.twitch.tv/zloyn",
        "shorts_suffix": "twitch: ZLOYn https://www.twitch.tv/zloyn",
        "reels": "twitch: ZLOYn https://www.twitch.tv/zloyn",
        "upload_note": "Twitch Злого (https://www.twitch.tv/zloyn) также должен быть указан в профиле "
                       "аккаунта, с которого публикуется ролик.",
        # в режиме «все категории» — не игры, а просмотр видео
        "exclude_games": ["Watch Party", "Movies & TV", "Anime", "Movies & Series"],
    },
}
NEW_CHANNELS = ["evelone2004", "zloyn"]
DEFAULT_CATEGORY = CATEGORY


def channels_from_arg(arg):
    """'strogo' | 'evelone2004' | 'zloyn' | 'new' (оба новых) | 'all' (все три) | список через запятую."""
    a = (arg or "").strip().lower()
    if a in ("", "default"):
        return list(PROFILES)          # расписание: все каналы
    if a in ("new", "evelone2004+zloyn", "both"):
        return list(NEW_CHANNELS)
    if a == "all":
        return list(PROFILES)
    out = [c.strip() for c in a.split(",") if c.strip()]
    bad = [c for c in out if c not in PROFILES]
    if bad:
        raise ValueError(f"неизвестный канал: {', '.join(bad)}")
    return out


def apply_profile(login, category=None):
    """Переключить модульные параметры на канал (CHANNEL, CATEGORY, окно поиска, лимит)."""
    global CHANNEL, CATEGORY, DISCOVERY_HOURS, MAX_PER_DAY, PROFILE
    p = PROFILES[login]
    PROFILE = p
    CHANNEL = p["login"]
    CATEGORY = DEFAULT_CATEGORY if category in (None, "", "default") else category
    DISCOVERY_HOURS = p["discovery_hours"]
    MAX_PER_DAY = p["max_per_day"]
    return p


PROFILE = PROFILES["strogo"]
