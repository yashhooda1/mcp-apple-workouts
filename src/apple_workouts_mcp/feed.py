"""The workout feed: the hand-off between this server and HoodaRun.

The server never talks to the Watch. It keeps one JSON file, `workouts.json`,
listing every workout that should be on the calendar. HoodaRun reads it,
schedules what's new or changed through WorkoutKit, removes what disappeared,
and writes `status.json` back next to it so the server can report what really
landed on the Watch.

Where the feed lives (`WORKOUT_FEED_DIR`):
  * **iCloud Drive folder** (default on a Mac) — the server runs locally over
    stdio with Claude Desktop, writes into iCloud Drive, and the file appears on
    the iPhone. No server, no account, nothing third-party.
  * **Any directory + HTTP** — run the server with the HTTP transport and the
    app pulls `GET /feed.json` / posts `POST /status` behind the same bearer
    token as `/mcp` (see `feed_http.py`).

Ids are deterministic (`uuid5(date|name)`), so re-pushing a plan updates in
place instead of duplicating, and each item carries a content `rev` so the app
reschedules only what actually changed.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

FEED_SCHEMA = "hooda.workouts.feed/v1"
FEED_FILE = "workouts.json"
STATUS_FILE = "status.json"

# Fixed namespace so ids are stable across machines and reinstalls.
_NS = uuid.UUID("6f1c2b8e-5a0d-4c47-9e1f-2a7d0b3c9e55")

_ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def default_dir() -> Path:
    env = os.environ.get("WORKOUT_FEED_DIR")
    if env:
        return Path(env).expanduser()
    if _ICLOUD.is_dir():
        return _ICLOUD / "HoodaRun" / "MCP"
    return Path.home() / ".hooda-workouts"


def workout_id(date: str, name: str) -> str:
    return str(uuid.uuid5(_NS, f"{date}|{name}")).upper()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FeedStore:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else default_dir()

    @property
    def feed_path(self) -> Path:
        return self.dir / FEED_FILE

    @property
    def status_path(self) -> Path:
        return self.dir / STATUS_FILE

    # ------------------------------------------------------------------ io
    def load(self) -> dict:
        if not self.feed_path.exists():
            return {"schema": FEED_SCHEMA, "generated_at": None, "workouts": []}
        data = json.loads(self.feed_path.read_text())
        if data.get("schema") != FEED_SCHEMA:
            raise ValueError(f"{self.feed_path}: unexpected schema {data.get('schema')!r}")
        return data

    def _write_json(self, path: Path, data: dict) -> None:
        """Atomic write: iCloud must never sync a half-written file."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self, data: dict) -> None:
        data["schema"] = FEED_SCHEMA
        data["generated_at"] = _now()
        data["workouts"].sort(key=lambda w: (w["date"], w.get("time") or "", w["name"]))
        self._write_json(self.feed_path, data)

    # ------------------------------------------------------------ mutations
    def upsert(self, items: Iterable[dict]) -> list[dict]:
        """Insert or replace by id. Returns the stored records."""
        data = self.load()
        by_id = {w["id"]: w for w in data["workouts"]}
        stored = []
        for item in items:
            prev = by_id.get(item["id"])
            item = dict(item)
            item["created_at"] = prev["created_at"] if prev else _now()
            item["updated_at"] = _now() if (not prev or prev["rev"] != item["rev"]) \
                else prev["updated_at"]
            by_id[item["id"]] = item
            stored.append(item)
        data["workouts"] = list(by_id.values())
        self.save(data)
        return stored

    def remove(self, ids: Iterable[str]) -> list[dict]:
        ids = {i.upper() for i in ids}
        data = self.load()
        gone = [w for w in data["workouts"] if w["id"] in ids]
        data["workouts"] = [w for w in data["workouts"] if w["id"] not in ids]
        if gone:
            self.save(data)
        return gone

    # --------------------------------------------------------------- reads
    def between(self, start: Optional[str] = None, end: Optional[str] = None) -> list[dict]:
        rows = self.load()["workouts"]
        return [w for w in rows
                if (start is None or w["date"] >= start) and (end is None or w["date"] <= end)]

    def status(self) -> Optional[dict]:
        if not self.status_path.exists():
            return None
        return json.loads(self.status_path.read_text())

    def write_status(self, status: dict) -> None:
        self._write_json(self.status_path, status)
