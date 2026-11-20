#!/usr/bin/env python3
"""Поиск публичных клипов Twitch канала за последние N часов в категории.

Официальный Twitch Helix API:
  1. POST https://id.twitch.tv/oauth2/token        (app access token, client_credentials)
  2. GET  /helix/users?login=<канал>               -> broadcaster_id
  3. GET  /helix/games?name=<категория>            -> game_id
  4. GET  /helix/clips?broadcaster_id=..&started_at=..&ended_at=..  (с пагинацией)
Фильтр по game_id, сортировка по created_at (новые -> старые).

Результат (поле "status" в JSON и код выхода):
  found  — клипы найдены                         -> exit 0
  empty  — запрос успешен, клипов 0              -> exit 0
  error  — ошибка авторизации или API            -> exit 1

Секреты берутся из TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET и никогда не
печатаются; токен маскируется в логах GitHub Actions (::add-mask::).
Только стандартная библиотека Python.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_URL = os.environ.get("TWITCH_AUTH_URL", "https://id.twitch.tv/oauth2/token")
API_BASE = os.environ.get("TWITCH_API_BASE", "https://api.twitch.tv/helix")
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def load_dotenv(path):
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"{path}:{line_number}: unmatched quote")
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        os.environ.setdefault(key, value)


load_dotenv(PROJECT_ROOT / ".env")


class TwitchError(Exception):
    def __init__(self, kind, message, http_status=None):
        super().__init__(message)
        self.kind = kind                # "auth" | "api" | "config" | "network"
        self.http_status = http_status


def _safe_body(raw):
    """Короткое описание ошибки от Twitch без заголовков и секретов."""
    try:
        d = json.loads(raw)
        return d.get("message") or d.get("error") or str(d)[:200]
    except Exception:
        return raw[:200].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:200]


def _request(url, *, data=None, headers=None, what):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = _safe_body(e.read())
        kind = "auth" if e.code in (400, 401, 403) and what == "token" or e.code in (401, 403) else "api"
        raise TwitchError(kind, f"{what}: HTTP {e.code} — {body}", e.code) from None
    except urllib.error.URLError as e:
        raise TwitchError("network", f"{what}: сеть недоступна — {e.reason}") from None
    except json.JSONDecodeError:
        raise TwitchError("api", f"{what}: ответ не JSON") from None


def get_token(client_id, client_secret):
    data = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                   "grant_type": "client_credentials"}).encode()
    d = _request(AUTH_URL, data=data,
                 headers={"Content-Type": "application/x-www-form-urlencoded"}, what="token")
    tok = d.get("access_token")
    if not tok:
        raise TwitchError("auth", "token: в ответе нет access_token")
    if IN_ACTIONS:
        print(f"::add-mask::{tok}", flush=True)
    return tok


def helix(path, params, client_id, token):
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
    return _request(url, headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"},
                    what=f"GET /helix/{path}")


def fetch_clips(broadcaster_id, started, ended, client_id, token):
    clips, cursor, pages = [], None, 0
    while True:
        p = {"broadcaster_id": broadcaster_id, "first": 100,
             "started_at": started, "ended_at": ended}
        if cursor:
            p["after"] = cursor
        d = helix("clips", p, client_id, token)
        clips += d.get("data", [])
        cursor = (d.get("pagination") or {}).get("cursor")
        pages += 1
        if not cursor or not d.get("data") or pages >= 50:
            return clips


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_summary(report):
    lines = [f"## Twitch clips: `{report['channel']}` — {report['category']}", ""]
    lines.append(f"Окно поиска (UTC): {report['window']['started_at']} → {report['window']['ended_at']}")
    lines.append("")
    st = report["status"]
    if st == "found":
        lines.append(f"✅ **Найдено клипов: {report['count']}** (новые → старые)")
        lines += ["", "| # | Название | Создан (UTC) | Длит., с | Просмотры | Ссылка |",
                  "|---|---|---|---|---|---|"]
        for i, c in enumerate(report["clips"], 1):
            t = c["title"].replace("|", "\\|")
            lines.append(f"| {i} | {t} | {c['created_at']} | {c['duration']} | "
                         f"{c['view_count']} | {c['url']} |")
    elif st == "empty":
        lines.append("ℹ️ **Запрос успешен, клипов 0.**")
        lines.append(f"Всего клипов канала за окно: {report.get('total_in_window', 0)}, "
                     f"из них в категории: 0.")
        if report.get("note"):
            lines.append(f"Примечание: {report['note']}")
    else:
        lines.append(f"❌ **Ошибка ({report['error']['kind']}):** {report['error']['message']}")
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="strogo")
    ap.add_argument("--category", default="Grand Theft Auto VI")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--out", default="twitch_clips.json")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    report = {"status": None, "channel": a.channel, "category": a.category,
              "window": {"started_at": iso(now - timedelta(hours=a.hours)), "ended_at": iso(now)},
              "generated_at": iso(now), "count": 0, "clips": []}
    try:
        cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
        sec = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        if not cid or not sec:
            raise TwitchError("config", "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET не заданы")
        token = get_token(cid, sec)

        users = helix("users", {"login": a.channel}, cid, token).get("data", [])
        if not users:
            raise TwitchError("api", f"канал '{a.channel}' не найден")
        bid = users[0]["id"]
        report["broadcaster_id"] = bid

        games = helix("games", {"name": a.category}, cid, token).get("data", [])
        game_id = games[0]["id"] if games else None
        report["game_id"] = game_id

        all_clips = fetch_clips(bid, report["window"]["started_at"], report["window"]["ended_at"],
                                cid, token)
        report["total_in_window"] = len(all_clips)
        if game_id is None:
            report["note"] = f"категория '{a.category}' не найдена в Twitch"
        sel = [c for c in all_clips if game_id and c.get("game_id") == game_id]
        sel.sort(key=lambda c: c["created_at"], reverse=True)
        report["clips"] = [{
            "title": c.get("title", ""), "url": c.get("url", ""),
            "created_at": c.get("created_at", ""), "duration": c.get("duration"),
            "view_count": c.get("view_count"), "id": c.get("id"),
            "creator_name": c.get("creator_name"), "game_id": c.get("game_id"),
            "thumbnail_url": c.get("thumbnail_url"),
        } for c in sel]
        report["count"] = len(sel)
        report["status"] = "found" if sel else "empty"
    except TwitchError as e:
        report["status"] = "error"
        report["error"] = {"kind": e.kind, "message": str(e), "http_status": e.http_status}

    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    write_summary(report)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"status={report['status']}\ncount={report['count']}\n")
    sys.exit(1 if report["status"] == "error" else 0)


if __name__ == "__main__":
    main()
