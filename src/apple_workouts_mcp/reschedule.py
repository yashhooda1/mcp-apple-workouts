"""Moving workouts that are already published: one session, two swapped, or a
whole stretch of a plan.

A move keeps the workout's id. That is what makes it a *move* everywhere
downstream rather than a delete-and-recreate: the Apple Calendar event keeps
its UID and slides to the new day, and HoodaRun replaces the same scheduled
workout on the Watch.

Three things a move has to get right:

  * **HoodaRun must notice.** The app reschedules an item only when its `rev`
    changes, and `rev` is a hash of the workout's content, not its date. So a
    moved item's rev is re-derived from its original rev plus the new date
    (`base_rev` keeps the original so moving back restores it exactly).
  * **Re-publishing the plan must not undo it.** Generators rebuild every
    session on its planned date. A moved item remembers that date in
    `moved_from`; when the same session is published again, `keep_moves()`
    puts it back on the day the runner chose.
  * **Race day is not a training session.** Shifting a block later doesn't
    move the race unless asked, and the result says which sessions now land
    on or after it instead of quietly producing a plan that overruns its race.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Optional

from .feed import FeedStore, _now

MAX_SHIFT_DAYS = 180


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError(f"bad date {text!r}; use YYYY-MM-DD")


def _time(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    try:
        hh, mm = (int(x) for x in text.split(":"))
    except ValueError:
        raise ValueError(f"bad time {text!r}; use 24h 'HH:MM'")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"bad time {text!r}; use 24h 'HH:MM'")
    return f"{hh:02d}:{mm:02d}"


def is_race(w: dict) -> bool:
    """The race itself. The generators name it "RACE — …" in capitals; that is
    deliberately case-sensitive so "Race-week easy 3mi" is not mistaken for it."""
    return w["name"].startswith("RACE")


def _moved_rev(base_rev: str, new_date: str, time: Optional[str]) -> str:
    return hashlib.sha256(f"{base_rev}|{new_date}|{time or ''}".encode()).hexdigest()[:16]


def _place(w: dict, new_date: str, time: Optional[str] = None,
           set_time: bool = False) -> dict:
    """Return `w` on `new_date` (and `time`, when set_time), bookkeeping updated."""
    w = dict(w)
    origin = w.get("moved_from") or w["date"]
    base = w.get("base_rev") or w["rev"]
    if set_time and time != w.get("time"):
        w["time"], w["moved_time"] = time, True
    w["date"] = new_date
    if new_date == origin and not w.get("moved_time"):
        # Back where the plan put it: indistinguishable from never having moved.
        w["rev"] = base
        w.pop("moved_from", None)
        w.pop("base_rev", None)
    else:
        w["moved_from"], w["base_rev"] = origin, base
        w["rev"] = _moved_rev(base, new_date, w.get("time"))
    w["updated_at"] = _now()
    return w


def _save(store: FeedStore, changed: Iterable[dict]) -> None:
    data = store.load()
    by_id = {w["id"]: w for w in changed}
    data["workouts"] = [by_id.get(w["id"], w) for w in data["workouts"]]
    store.save(data)


def _find(store: FeedStore, workout_id: str) -> dict:
    wanted = (workout_id or "").upper()
    for w in store.load()["workouts"]:
        if w["id"] == wanted:
            return w
    raise ValueError(f"no scheduled workout with id {workout_id!r}; "
                     "list_scheduled shows the ids")


def brief(w: dict) -> dict:
    out = {"id": w["id"], "date": w["date"], "time": w.get("time"), "name": w["name"],
           "sport": w["sport"], "plan": w.get("plan")}
    if w.get("moved_from"):
        out["planned_date"] = w["moved_from"]
    return out


def _doubled_days(workouts: Iterable[dict], dates: set[str]) -> list[dict]:
    """Days in `dates` that now hold more than one run (strength alongside is normal)."""
    runs = defaultdict(list)
    for w in workouts:
        if w["date"] in dates and w["sport"] != "strength":
            runs[w["date"]].append(w["name"])
    return [{"date": d, "runs": names} for d, names in sorted(runs.items()) if len(names) > 1]


# ----------------------------------------------------------------- one workout
def move(store: FeedStore, workout_id: str, new_date: str,
         time: Optional[str] = None) -> dict:
    """Move one workout to `new_date`, optionally changing its start time."""
    w = _find(store, workout_id)
    target = _day(new_date).isoformat()
    moved = _place(w, target, _time(time), set_time=time is not None)
    if moved["date"] == w["date"] and moved.get("time") == w.get("time"):
        return {"moved": False, "note": "already there", "workout": brief(w)}
    _save(store, [moved])
    return {"moved": True, "from": w["date"], "workout": brief(moved),
            "same_day": [brief(o) for o in store.between(target, target)
                         if o["id"] != moved["id"]]}


def swap(store: FeedStore, first_id: str, second_id: str) -> dict:
    """Trade the days (and start times) of two workouts."""
    a, b = _find(store, first_id), _find(store, second_id)
    if a["id"] == b["id"]:
        raise ValueError("pick two different workouts to swap")
    new_a = _place(a, b["date"], b.get("time"), set_time=a.get("time") != b.get("time"))
    new_b = _place(b, a["date"], a.get("time"), set_time=a.get("time") != b.get("time"))
    _save(store, [new_a, new_b])
    return {"swapped": [brief(new_a), brief(new_b)]}


# ------------------------------------------------------------ a stretch of plan
def shift(store: FeedStore, days: int, start_date: Optional[str] = None,
          end_date: Optional[str] = None, plan: Optional[str] = None,
          move_race: bool = False, dry_run: bool = True,
          today: Optional[date] = None) -> dict:
    """Slide every workout dated start_date..end_date by `days` (negative = earlier).

    `plan` narrows it to workouts whose plan name contains that text. Race-day
    sessions stay put unless move_race=True.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days == 0 \
            or abs(days) > MAX_SHIFT_DAYS:
        raise ValueError(f"days must be a whole number from 1 to {MAX_SHIFT_DAYS}, "
                         "negative to move earlier")
    today = today or date.today()
    start = _day(start_date) if start_date else today
    end = _day(end_date) if end_date else None
    if end and end < start:
        raise ValueError("end_date is before start_date")
    needle = (plan or "").strip().lower()

    everything = store.load()["workouts"]
    chosen = [w for w in everything
              if w["date"] >= start.isoformat()
              and (end is None or w["date"] <= end.isoformat())
              and (not needle or needle in (w.get("plan") or "").lower())]
    races_kept = [w for w in chosen if is_race(w) and not move_race]
    moving = [w for w in chosen if not (is_race(w) and not move_race)]
    if not moving:
        return {"dry_run": dry_run, "shifted": 0, "note": "nothing to shift in that range",
                "race_days_kept": [brief(w) for w in races_kept]}

    earliest = min(_day(w["date"]) for w in moving) + timedelta(days=days)
    if earliest < today:
        raise ValueError(f"that would put workouts in the past (earliest "
                         f"{earliest.isoformat()}); shift by fewer days or start later")

    moved = [_place(w, (_day(w["date"]) + timedelta(days=days)).isoformat()) for w in moving]
    by_id = {w["id"]: w for w in moved}
    after = [by_id.get(w["id"], w) for w in everything]

    # A plan's race day: sessions of that plan landing on or after it are a problem.
    race_day = {w.get("plan"): w["date"] for w in after if is_race(w) and w.get("plan")}
    past_race = [w for w in moved if not is_race(w)
                 and w.get("plan") in race_day and w["date"] >= race_day[w["plan"]]]
    out = {
        "dry_run": dry_run, "days": days,
        "range": [start.isoformat(), end.isoformat() if end else None],
        "shifted": len(moved),
        "first": brief(min(moved, key=lambda w: w["date"])),
        "last": brief(max(moved, key=lambda w: w["date"])),
        "race_days_kept": [brief(w) for w in races_kept],
        "now_on_or_after_race_day": [brief(w) for w in past_race],
        "doubled_days": _doubled_days(after, {w["date"] for w in moved}),
    }
    if past_race:
        out["warning"] = (
            f"{len(past_race)} session(s) would land on or after race day. If the race "
            "date hasn't changed, don't shift the whole plan: clear the days you missed "
            "and keep the rest, or shift a shorter range with end_date. If the race "
            "itself moved, use move_race=True.")
    if not dry_run:
        _save(store, moved)
    return out


# ------------------------------------------------------------- re-publishing
def keep_moves(store: FeedStore, items: list[dict]) -> list[dict]:
    """Before a plan is re-published: put sessions the runner moved back on the
    day they chose. Returns the ones that were kept."""
    existing = {w["id"]: w for w in store.load()["workouts"]}
    kept = []
    for i, item in enumerate(items):
        prev = existing.get(item["id"])
        if not prev or prev.get("moved_from") != item["date"]:
            continue
        placed = dict(item)
        if prev.get("moved_time"):
            placed["time"], placed["moved_time"] = prev.get("time"), True
        placed = _place(placed, prev["date"])
        items[i] = placed
        kept.append(placed)
    return kept
