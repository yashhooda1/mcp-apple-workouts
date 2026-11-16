"""Workouts the runner actually did, reported back by HoodaRun from Health.

Until this existed the server only ever sent plans out. HoodaRun now reads
finished workouts from HealthKit on the iPhone (the runner switches this on in
the app) and reports them the same way it reports sync status:

  * **HTTP**   `POST /completed` with {"workouts": [...]}, behind the same
               bearer gate, so each account's results land in its own folder;
  * **iCloud** the app writes `completed.json` next to `workouts.json`.

Rows use the shape `fitness.summarize_fitness` already reads (the one Strava
activities are reduced to), so a runner's own watch data can drive fitness
assessment and plan building with no Strava connection at all:

    id, type, start (local ISO), distance_m, duration_s, avg_hr
    + max_hr, elevation_gain_m, end, source

This is health data. It is stored only in the runner's own feed folder, is
never shared between accounts, and is capped in age and count.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from .feed import FeedStore, _now
from .fitness import M_PER_MILE

COMPLETED_SCHEMA = "hooda.workouts.completed/v1"
COMPLETED_FILE = "completed.json"
MAX_AGE_DAYS = 400
MAX_ROWS = 1500
MAX_BATCH = 500
TYPES = {"run": "Run", "running": "Run", "trailrun": "Run", "walk": "Walk",
         "walking": "Walk", "hike": "Hike", "hiking": "Hike", "strength": "Strength",
         "weighttraining": "Strength", "core": "Strength", "ride": "Ride",
         "cycling": "Ride", "other": "Other"}


def _number(value, low: float, high: float) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None   # also drops NaN


def normalize(raw: dict) -> Optional[dict]:
    """One reported workout -> a stored row, or None if it isn't usable."""
    if not isinstance(raw, dict):
        return None
    ident = str(raw.get("id") or "").strip()[:64]
    try:
        start = datetime.fromisoformat(str(raw.get("start", "")).replace("Z", ""))
    except ValueError:
        return None
    start = start.replace(tzinfo=None)   # local wall-clock time; the day is what matters
    duration = _number(raw.get("duration_s"), 30, 3 * 86400)
    if not ident or duration is None or abs((datetime.now() - start).days) > MAX_AGE_DAYS + 2:
        return None
    kind = TYPES.get(str(raw.get("type") or "other").lower().replace(" ", ""), "Other")
    row = {"id": ident, "type": kind, "start": start.isoformat(timespec="seconds"),
           "date": start.date().isoformat(), "duration_s": round(duration)}
    for key, low, high, digits in (("distance_m", 0, 500_000, 0), ("avg_hr", 25, 250, 0),
                                   ("max_hr", 25, 250, 0), ("elevation_gain_m", 0, 20_000, 0)):
        n = _number(raw.get(key), low, high)
        if n is not None:
            row[key] = round(n, digits)
    for key in ("source", "name"):
        if raw.get(key):
            row[key] = str(raw[key])[:80]
    return row


def _path(store: FeedStore):
    return store.dir / COMPLETED_FILE


def load(store: FeedStore) -> list[dict]:
    """Every stored row, oldest first."""
    path = _path(store)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    rows = data.get("workouts", []) if isinstance(data, dict) else []
    # The iCloud path is written by the app, not by record(): normalise on read.
    clean = [r for r in (normalize(r) for r in rows) if r]
    return sorted(clean, key=lambda r: r["start"])


def record(store: FeedStore, reported: Iterable) -> dict:
    """Merge a batch from the app (same id = replace). Returns a summary."""
    reported = list(reported)[:MAX_BATCH]
    fresh = [r for r in (normalize(x) for x in reported) if r]
    by_id = {r["id"]: r for r in load(store)}
    new = sum(1 for r in fresh if r["id"] not in by_id)
    by_id.update({r["id"]: r for r in fresh})
    cutoff = (date.today() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    rows = sorted((r for r in by_id.values() if r["date"] >= cutoff),
                  key=lambda r: r["start"])[-MAX_ROWS:]
    store._write_json(_path(store), {"schema": COMPLETED_SCHEMA, "updated_at": _now(),
                                     "workouts": rows})
    return {"ok": True, "received": len(reported), "accepted": len(fresh), "new": new,
            "stored": len(rows), "latest_start": rows[-1]["start"] if rows else None}


def between(store: FeedStore, start: str, end: str) -> list[dict]:
    return [r for r in load(store) if start <= r["date"] <= end]


def recent(store: FeedStore, days: int, today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    return between(store, (today - timedelta(days=days)).isoformat(), today.isoformat())


def view(row: dict) -> dict:
    """A row with the numbers a person reads: miles and pace."""
    out = {"date": row["date"], "start": row["start"][11:16], "type": row["type"],
           "minutes": round(row["duration_s"] / 60, 1)}
    meters = row.get("distance_m") or 0
    if meters > 0:
        miles = meters / M_PER_MILE
        out["miles"] = round(miles, 2)
        if row["type"] in ("Run", "Walk", "Hike") and miles >= 0.1:
            pace = round(row["duration_s"] / miles)
            out["pace_per_mile"] = f"{int(pace // 60)}:{int(pace % 60):02d}"
    for key in ("avg_hr", "max_hr"):
        if key in row:
            out[key] = int(row[key])
    if row.get("elevation_gain_m"):
        out["climb_ft"] = round(row["elevation_gain_m"] * 3.28084)
    return {**out, "id": row["id"]}
