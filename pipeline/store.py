"""Постоянное хранилище состояния: JSON в отдельной ветке git (pipeline-state).

Последовательное обновление без потери запусков:
  * каждое сохранение — commit + push; если push отклонён (другой запуск успел раньше),
    берём свежую версию ветки, накладываем свои изменения и пробуем снова;
  * перед оценкой и монтажом клип «занимается» (claim) таким же атомарным push —
    если его уже взял другой запуск или момент уже смонтирован, клип пропускается.
Так ручной и плановый запуски не смонтируют один момент дважды, даже если идут одновременно.
"""
import copy
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from . import state as ST

CLAIM_TTL_MIN = 45


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _foreign_claim(rec, run_id):
    c = rec.get("claim")
    if not c or c.get("run") == run_id:
        return False
    return _now() - datetime.fromisoformat(c["at"].replace("Z", "+00:00")) < timedelta(minutes=CLAIM_TTL_MIN)


def merge(remote, mine, touched):
    """Свежая версия из ветки + изменения этого запуска."""
    out = copy.deepcopy(remote)
    for cid, rec in mine["clips"].items():
        r = out["clips"].get(cid)
        forced = rec.get("force_run") is not None and rec.get("force_run") == mine.get("_run_id")
        protected = r is not None and not forced and (
            (r.get("status") == "rendered" and rec.get("status") != "rendered")
            or _foreign_claim(r, mine.get("_run_id")))
        if r is None or (cid in touched and not protected):
            out["clips"][cid] = copy.deepcopy(rec)
            continue
        hist = {h[0]: h for h in (r.get("views_history") or []) + (rec.get("views_history") or [])}
        r["views_history"] = [hist[k] for k in sorted(hist)][-60:]
        r["views"] = max(r.get("views", 0), rec.get("views", 0))
        for k in ("title", "duration", "video_id", "vod_offset", "url", "created_at", "game_id"):
            if rec.get(k) is not None:
                r[k] = rec[k]
    out["same_event"] = sorted({tuple(p) for p in remote.get("same_event", []) + mine.get("same_event", [])})
    out["same_event"] = [list(p) for p in out["same_event"]]
    out["checked_pairs"] = sorted(set(remote.get("checked_pairs", [])) | set(mine.get("checked_pairs", [])))
    for k in ("stream", "game_id", "updated_at"):
        if k in mine:
            out[k] = mine[k]
    for dkey, d in mine.get("days", {}).items():
        od = out["days"].setdefault(dkey, {"evaluated": 0, "rendered": [], "finalized": False})
        od["finalized"] = od.get("finalized") or d.get("finalized")
    # счётчики суток — из самих записей (не теряются при слиянии)
    from .select import local_day
    for od in out["days"].values():
        od["evaluated"] = 0
        od["rendered"] = []
    for cid, r in out["clips"].items():
        if r.get("evaluated_at"):
            ST.day(out, local_day(r["evaluated_at"]))["evaluated"] += 1
        if r.get("status") == "rendered":
            ST.day(out, local_day(r["created_at"]))["rendered"].append(cid)
    out["runs"] = (remote.get("runs", []) + [x for x in mine.get("runs", []) if x not in remote.get("runs", [])])[-50:]
    out.pop("manual", None)
    out.pop("_run_id", None)
    if "manual" in mine:
        out["manual"] = mine["manual"]
    return out


class Store:
    def __init__(self, path, channel, remote=None, branch=None, readonly=False, run_id=None):
        self.path = path
        self.dir = os.path.dirname(path) or "."
        self.channel = channel
        self.remote = remote
        self.branch = branch
        self.readonly = readonly
        self.run_id = run_id or os.environ.get("GITHUB_RUN_ID", "local")
        self.touched = set()

    # --- git ---
    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.dir, *args], capture_output=True, text=True, check=check)

    def _sync(self):
        """Ветка состояния → рабочая папка (создаёт ветку при первом запуске)."""
        if not self.remote:
            return
        if not os.path.isdir(os.path.join(self.dir, ".git")):
            os.makedirs(self.dir, exist_ok=True)
            self._git("init", "-q", "-b", self.branch)
            self._git("remote", "add", "origin", self.remote)
            self._git("config", "user.name", "github-actions[bot]")
            self._git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        r = self._git("fetch", "-q", "origin", self.branch, check=False)
        if r.returncode == 0:
            self._git("checkout", "-q", "-B", self.branch, "FETCH_HEAD")
            self._git("reset", "-q", "--hard", "FETCH_HEAD")

    def _push(self, state, msg):
        ST.save(self.path, state)
        if not self.remote:
            return True
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True
        self._git("commit", "-q", "-m", msg)
        r = self._git("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", check=False)
        return r.returncode == 0

    # --- API ---
    def load(self):
        self._sync()
        return ST.load(self.path, self.channel)

    def save(self, state, msg="state"):
        if self.readonly:
            return state
        for _ in range(6):
            self._sync()
            remote = ST.load(self.path, self.channel)
            state["_run_id"] = self.run_id
            merged = merge(remote, state, self.touched)
            if self._push(merged, f"{msg} (run {self.run_id})"):
                state.clear()
                state.update(merged)
                return state
        raise RuntimeError("не удалось сохранить состояние: ветка постоянно меняется")

    def claim(self, state, cid, allowed, group_ok=None):
        """Атомарно занять клип. allowed — допустимые статусы в свежей версии ветки."""
        if self.readonly:
            return True
        for _ in range(6):
            self._sync()
            remote = ST.load(self.path, self.channel)
            r = remote["clips"].get(cid) or state["clips"].get(cid)
            if r is None or r.get("status") not in allowed:
                return False
            if _foreign_claim(r, self.run_id):
                return False
            if group_ok and not group_ok(remote):
                return False
            mine = copy.deepcopy(state)
            mine["clips"][cid] = copy.deepcopy(state["clips"].get(cid) or r)
            mine["clips"][cid]["status"] = r.get("status")
            mine["clips"][cid]["claim"] = {"run": self.run_id, "at": _iso(_now())}
            mine["_run_id"] = self.run_id
            self.touched.add(cid)
            merged = merge(remote, mine, self.touched)
            if self._push(merged, f"claim {cid} (run {self.run_id})"):
                state.clear()
                state.update(merged)
                return True
        return False

    def release(self, state, cid):
        rec = state["clips"].get(cid)
        if rec and rec.get("claim", {}).get("run") == self.run_id:
            rec.pop("claim", None)
        self.touched.add(cid)


def dump(state):
    return json.dumps(state, ensure_ascii=False)
