"""How the week went, and changing what's ahead because of it.

Two halves, both working on what is already published rather than
regenerating a plan:

**review_week** lines the planned sessions up against the workouts HoodaRun
reported from Health (completed.py): what was done, what was cut short, what
was missed, what was extra — and a plain reading of it.

**adjust** changes upcoming sessions in place:

    ease     the first days back after illness, time off or a rough week:
             quality sessions become easy runs and every run gets shorter
    scale    more or less easy and long-run volume; workouts keep their shape
    repace   quality-session pace targets move slower or faster by N s/mile,
             after a race or time trial shows the plan's paces are off

An adjusted session keeps its id (same calendar event, same Watch slot), gets
a new rev so HoodaRun reschedules it, and records what it was (`adjusted`).
Race day is never touched. Publishing the plan again restores the generated
sessions — an adjustment is a short-term correction, not a new plan.

What this can and can't see: Health reports one row per workout — total time,
distance, average heart rate. So a review knows a workout happened and how far
it went; it does not know whether the 4×1 mile reps were on pace.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import completed
from .apple import compile_any, parse_spec, revision
from .feed import FeedStore, _now
from .fitness import M_PER_MILE
from .reschedule import _moved_rev, _save, brief, is_race
from .workouts import easy

PARTIAL_BELOW = 0.8      # under 80% of the planned distance or time = cut short
BEHIND_BELOW = 0.7       # under 70% of the week's planned miles = a real shortfall
OVER_ABOVE = 1.15
_MILES_IN_NAME = re.compile(r"(\d+(?:\.\d+)?)mi\b")


# ---------------------------------------------------------------- what was planned
def planned_miles(workout: dict) -> float:
    """Miles the session asks for in distance-based steps (timed recoveries and
    timed runs add a little more that can't be known in advance)."""
    def meters(step) -> float:
        goal = (step or {}).get("goal") or {}
        return goal.get("meters", 0.0) if goal.get("type") == "distance" else 0.0

    total = meters(workout.get("warmup")) + meters(workout.get("cooldown"))
    for block in workout.get("blocks", []):
        total += block.get("iterations", 1) * sum(meters(s["step"]) for s in block["steps"])
    return total / M_PER_MILE


def kind_of(w: dict) -> str:
    if w["sport"] == "strength":
        return "strength"
    if is_race(w):
        return "race"
    name = w["name"].lower()
    if name.startswith("long"):
        return "long"
    wk = w["workout"]
    has_reps = any(b.get("iterations", 1) > 1 and
                   any((s["step"].get("alert") or {}).get("type") == "speed"
                       for s in b["steps"]) for b in wk.get("blocks", []))
    return "quality" if has_reps or wk.get("warmup") else "easy"


# ------------------------------------------------------------------- week review
def _week(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def _status(planned_mi: float, planned_s: float, done: dict) -> str:
    if planned_mi >= 0.5 and done.get("distance_m"):
        ratio = (done["distance_m"] / M_PER_MILE) / planned_mi
    elif planned_s:
        ratio = done["duration_s"] / planned_s
    else:
        return "done"
    return "done" if ratio >= PARTIAL_BELOW else "cut short"


def review_week(store: FeedStore, week_of: Optional[str] = None,
                today: Optional[date] = None) -> dict:
    today = today or date.today()
    monday, sunday = _week(date.fromisoformat(week_of) if week_of else today)
    planned = store.between(monday.isoformat(), sunday.isoformat())
    done = completed.between(store, monday.isoformat(), sunday.isoformat())
    out: dict = {"week": [monday.isoformat(), sunday.isoformat()],
                 "in_progress": monday <= today <= sunday}
    if not completed.load(store):
        out["note"] = ("No completed workouts have been reported, so there is nothing to "
                       "compare the plan with. In HoodaRun: Claude plans › Share completed "
                       "workouts, then sync.")
        out["planned"] = [brief(w) for w in planned]
        return out

    # Pair each planned session with a same-day workout of the same sort; when a
    # day has two of either, the closest in distance pair up first.
    free = defaultdict(list)
    for row in done:
        sort = "strength" if row["type"] == "Strength" else \
            "run" if row["type"] in ("Run", "Walk", "Hike") else None
        if sort:
            free[(row["date"], sort)].append(row)

    sessions = []
    totals = {"planned_miles": 0.0, "planned_miles_so_far": 0.0, "done_miles": 0.0}
    for w in sorted(planned, key=lambda w: (w["date"], -planned_miles(w["workout"]))):
        kind = kind_of(w)
        miles = round(planned_miles(w["workout"]), 1)
        entry = {"date": w["date"], "name": w["name"], "kind": kind, "id": w["id"]}
        if kind != "strength":
            entry["planned_miles"] = miles
            totals["planned_miles"] += miles
            if w["date"] <= today.isoformat():
                totals["planned_miles_so_far"] += miles
        pool = free[(w["date"], "strength" if kind == "strength" else "run")]
        if pool:
            match = min(pool, key=lambda r: abs((r.get("distance_m", 0) / M_PER_MILE) - miles))
            pool.remove(match)
            entry["status"] = _status(miles if kind != "strength" else 0,
                                      w["workout"].get("estimated_seconds", 0), match)
            entry["actual"] = completed.view(match)
        elif w["date"] > today.isoformat():
            entry["status"] = "upcoming"
        elif w["date"] == today.isoformat():
            entry["status"] = "not yet today"
        else:
            entry["status"] = "missed"
        sessions.append(entry)
    sessions.sort(key=lambda e: (e["date"], e["kind"] == "strength"))

    extra = [completed.view(r) for pool in free.values() for r in pool]
    run_rows = [r for r in done if r["type"] == "Run"]
    totals["done_miles"] = sum(r.get("distance_m", 0) for r in run_rows) / M_PER_MILE
    due = [s for s in sessions if s["status"] in ("done", "cut short", "missed")]

    def tally(kind: str) -> str:
        mine = [s for s in due if s["kind"] == kind]
        return f"{sum(s['status'] != 'missed' for s in mine)} of {len(mine)}"

    out.update({
        "summary": {
            "planned_miles": round(totals["planned_miles"], 1),
            "planned_miles_so_far": round(totals["planned_miles_so_far"], 1),
            "done_miles": round(totals["done_miles"], 1),
            "runs_done": len(run_rows),
            "quality_sessions": tally("quality"),
            "long_run": next((s["status"] for s in sessions if s["kind"] == "long"), "none planned"),
            "strength_sessions": tally("strength"),
            "missed": [f"{s['date']} {s['name']}" for s in due if s["status"] == "missed"],
            "cut_short": [f"{s['date']} {s['name']}" for s in due if s["status"] == "cut short"],
        },
        "sessions": sessions,
        "unplanned": sorted(extra, key=lambda v: (v["date"], v["start"])),
    })
    out["reading"] = _reading(out["summary"], out["in_progress"], bool(planned))
    return out


def _reading(s: dict, in_progress: bool, anything_planned: bool) -> dict:
    """A cautious interpretation, and which adjust action (if any) fits."""
    if not anything_planned:
        return {"verdict": "nothing was planned this week", "suggest": None}
    target, done = s["planned_miles_so_far"], s["done_miles"]
    if target < 1:
        return {"verdict": "nothing was due yet", "suggest": None}
    ratio = done / target
    when = "so far this week" if in_progress else "for the week"
    if ratio < BEHIND_BELOW:
        return {
            "verdict": f"well short of plan {when}: {done:g} of {target:g} miles",
            "suggest": {"action": "ease", "factor": 0.8, "days": 7},
            "why": "Don't make up missed runs. Ease the next week back in — shorter runs, "
                   "no hard sessions — then pick the plan up where it is. If this was "
                   "illness or injury, ease for longer and by more.",
        }
    if ratio > OVER_ABOVE:
        return {
            "verdict": f"more than planned {when}: {done:g} against {target:g} miles",
            "suggest": None,
            "why": "Extra volume on top of a plan that is already ramping is the usual "
                   "route to injury. Hold to the plan next week rather than adding to it.",
        }
    if s["cut_short"] or s["missed"]:
        return {"verdict": f"close to plan {when} ({done:g} of {target:g} miles), "
                           "with a session or two missed or cut short",
                "suggest": None,
                "why": "One missed or shortened session doesn't need a change of plan."}
    return {"verdict": f"on plan {when}: {done:g} of {target:g} miles", "suggest": None}


# --------------------------------------------------------------------- adjusting
ACTIONS = ("ease", "scale", "repace")
LIMITS = {"ease": (0.4, 1.0), "scale": (0.5, 1.15)}
MAX_REPACE = (-20, 60)   # seconds per mile: faster is bounded tighter than slower


def _scaled_name(name: str, old: float, new: float) -> str:
    """'Long easy 10.1mi' -> 'Long easy 8.1mi' when the name states the distance."""
    def swap(m):
        return f"{new:g}mi" if abs(float(m.group(1)) - old) < 0.06 else m.group(0)
    return _MILES_IN_NAME.sub(swap, name, count=1)


def _scale_spec(spec: dict, factor: float) -> tuple[dict, bool]:
    """Scale the distance of plain running steps (not the reps inside a repeat)."""
    spec = {**spec, "steps": [dict(s) for s in spec["steps"]]}
    changed = False
    for step in spec["steps"]:
        text = str(step.get("duration", "")).lower()
        if "repeat" in step or step.get("kind") not in ("run", "warmup", "cooldown") \
                or not text.endswith("mi"):
            continue
        old = float(text[:-2])
        new = max(1.0, round(old * factor, 1))
        if new != old:
            step["duration"] = f"{new:g}mi"
            spec["name"] = _scaled_name(spec["name"], old, new)
            changed = True
    if changed and spec.get("estimated_minutes"):
        spec["estimated_minutes"] = max(10, round(spec["estimated_minutes"] * factor))
    return spec, changed


def _shift_pace(target: str, seconds: int) -> str:
    def shift(mmss: str) -> str:
        m, s = mmss.split(":")
        total = max(180, int(m) * 60 + int(s) + seconds)
        return f"{total // 60}:{total % 60:02d}"
    lo, hi = target.split(":", 1)[1].split("-")
    return f"pace:{shift(lo)}-{shift(hi)}"


def _repace_spec(spec: dict, seconds: int) -> tuple[dict, bool]:
    changed = False

    def walk(steps):
        nonlocal changed
        out = []
        for step in steps:
            step = dict(step)
            if "repeat" in step:
                step["steps"] = walk(step["steps"])
            elif str(step.get("target") or "").lower().startswith("pace:"):
                step["target"] = _shift_pace(step["target"].lower(), seconds)
                changed = True
            out.append(step)
        return out
    return {**spec, "steps": walk(spec["steps"])}, changed


def _rebuilt(w: dict, spec, note: dict) -> dict:
    """`w` with new content: same id, day and plan; new workout, name and rev."""
    workout = compile_any(spec)
    new = dict(w)
    new.update({"name": spec.name, "description": spec.description, "workout": workout,
                "spec": spec.model_dump(exclude_none=True), "updated_at": _now()})
    base = revision(workout, spec.description, w.get("time"))
    if w.get("moved_from"):          # keep a move's bookkeeping consistent
        new["base_rev"] = base
        new["rev"] = _moved_rev(base, w["date"], w.get("time"))
    else:
        new["rev"] = base
    first = (w.get("adjusted") or {}).get("was", w["name"])
    new["adjusted"] = {**note, "was": first, "at": _now()}
    return new


def adjust(store: FeedStore, action: str, start_date: Optional[str] = None,
           days: Optional[int] = None, plan: Optional[str] = None,
           factor: Optional[float] = None,
           seconds_per_mile: Optional[int] = None, dry_run: bool = True,
           today: Optional[date] = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    today = today or date.today()
    start = date.fromisoformat(start_date) if start_date else today
    if start < today:
        raise ValueError("start_date is in the past; only upcoming sessions can change")
    if days is None:
        days = 400 if action == "repace" else 7   # paces: the rest of the plan
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 400:
        raise ValueError("days must be between 1 and 400")
    end = start + timedelta(days=days - 1)

    if action == "repace":
        low, high = MAX_REPACE
        if not isinstance(seconds_per_mile, int) or isinstance(seconds_per_mile, bool) \
                or seconds_per_mile == 0 or not low <= seconds_per_mile <= high:
            raise ValueError(f"repace needs seconds_per_mile from {low} (faster) to "
                             f"+{high} (slower), not 0")
    else:
        low, high = LIMITS[action]
        factor = 0.8 if factor is None and action == "ease" else factor
        if factor is None or not low <= factor <= high:
            raise ValueError(f"{action} needs a factor between {low} and {high}")

    needle = (plan or "").strip().lower()
    window = [w for w in store.between(start.isoformat(), end.isoformat())
              if w["sport"] != "strength" and not is_race(w)
              and (not needle or needle in (w.get("plan") or "").lower())]
    changed, skipped = [], []
    for w in window:
        kind = kind_of(w)
        if action == "ease" and kind == "quality":
            miles = max(2.0, round(planned_miles(w["workout"]) * factor, 1))
            spec = easy(f"Easy {miles:g}mi (instead of {w['name']})", miles)
            spec.description = ("Eased back: an easy run in place of the planned workout. "
                                "Conversational effort; stop if anything feels wrong.")
            changed.append(_rebuilt(w, spec, {"action": "ease", "factor": factor}))
            continue
        if action in ("ease", "scale"):
            if kind == "quality":           # scale leaves a workout's structure alone
                skipped.append(w)
                continue
            new_spec, did = _scale_spec(w["spec"], factor)
            note = {"action": action, "factor": factor}
        else:
            new_spec, did = _repace_spec(w["spec"], seconds_per_mile)
            note = {"action": "repace", "seconds_per_mile": seconds_per_mile}
        if did:
            changed.append(_rebuilt(w, parse_spec(new_spec), note))
        else:
            skipped.append(w)

    before = {w["id"]: w for w in window}
    out = {
        "dry_run": dry_run, "action": action,
        "range": [start.isoformat(), end.isoformat()],
        "changed": [{"date": w["date"], "was": before[w["id"]]["name"], "now": w["name"],
                     "id": w["id"]} for w in changed],
        "unchanged": [{"date": w["date"], "name": w["name"]} for w in skipped],
        "miles_before": round(sum(planned_miles(before[w["id"]]["workout"]) for w in changed)
                              + sum(planned_miles(w["workout"]) for w in skipped), 1),
        "miles_after": round(sum(planned_miles(w["workout"]) for w in changed)
                             + sum(planned_miles(w["workout"]) for w in skipped), 1),
        "note": "Strength sessions and race day are never changed. Publishing the plan "
                "again restores the generated sessions.",
    }
    if not dry_run and changed:
        _save(store, changed)
    return out
