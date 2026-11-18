"""Twitch Helix: переиспользует scripts/find_twitch_clips.py (токен, запросы, пагинация клипов)."""
import importlib.util
import os
import re
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "find_twitch_clips", os.path.join(_HERE, "..", "scripts", "find_twitch_clips.py"))
ftc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ftc)

TwitchError = ftc.TwitchError
CLIP_RE = re.compile(r"(?:clips\.twitch\.tv/|/clip/)([A-Za-z0-9_-]+)")


def clip_id_from_url(url):
    m = CLIP_RE.search(url or "")
    return m.group(1) if m else None


class Twitch:
    def __init__(self):
        self.cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
        sec = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        if not self.cid or not sec:
            raise TwitchError("config", "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET не заданы")
        self.token = ftc.get_token(self.cid, sec)

    def get(self, path, params):
        return ftc.helix(path, params, self.cid, self.token)

    def user_id(self, login):
        data = self.get("users", {"login": login}).get("data", [])
        if not data:
            raise TwitchError("api", f"канал '{login}' не найден")
        return data[0]["id"]

    def game_id(self, name):
        data = self.get("games", {"name": name}).get("data", [])
        return data[0]["id"] if data else None

    def clips(self, broadcaster_id, hours):
        now = datetime.now(timezone.utc)
        return ftc.fetch_clips(broadcaster_id, ftc.iso(now - timedelta(hours=hours)), ftc.iso(now),
                               self.cid, self.token)

    def clip(self, clip_id):
        data = self.get("clips", {"id": clip_id}).get("data", [])
        return data[0] if data else None

    def stream(self, login):
        data = self.get("streams", {"user_login": login}).get("data", [])
        return data[0] if data else None
