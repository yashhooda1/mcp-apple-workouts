"""Training-plan engine: take a list of (date, spec) and publish them to the
workout feed as scheduled, structured Apple workouts.

A spec is either a `WorkoutSpec` (running) or a `StrengthSpec` (strength/core),
so a plan can interleave runs and lifts on the same calendar. Every generated
plan in this repo carries two strength sessions a week.

Idempotent: ids are `uuid5(date|name)`, so re-pushing updates in place. With
replace=True, items from an earlier push of the *same plan name* that are no
longer in the plan are removed, so an edited plan never leaves ghosts behind.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple, Union

from .apple import compile_any, revision, sport_of, step_count
from .feed import FeedStore, workout_id
from .strength import StrengthSpec
from .workouts import WorkoutSpec

# Mon..Sun offsets for readability
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

AnySpec = Union[WorkoutSpec, StrengthSpec]
PlanItem = Tuple[str, AnySpec]  # (ISO date, spec)


@dataclass
class TrainingPlan:
    name: str
    items: List[PlanItem]

    def dates(self) -> List[str]:
        return [d for d, _ in self.items]


def week(monday: str, *sessions: Tuple[int, AnySpec]) -> List[PlanItem]:
    """Expand (weekday_offset, spec) pairs anchored on a Monday into dated items."""
    base = date.fromisoformat(monday)
    return [((base + timedelta(days=off)).isoformat(), spec) for off, spec in sessions]


def _check_date(iso: str) -> str:
    return date.fromisoformat(iso).isoformat()   # raises on '2026-13-40'


def _check_time(t: Optional[str]) -> Optional[str]:
    if t is None:
        return None
    hh, mm = (int(x) for x in t.split(":"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"bad time {t!r}; use 24h 'HH:MM'")
    return f"{hh:02d}:{mm:02d}"


def feed_item(iso_date: str, spec: AnySpec, time: Optional[str] = None,
              plan: Optional[str] = None) -> dict:
    """Compile one spec into a feed record."""
    iso_date, time = _check_date(iso_date), _check_time(time)
    workout = compile_any(spec)
    return {
        "id": workout_id(iso_date, spec.name),
        "rev": revision(workout, spec.description, time),
        "date": iso_date,
        "time": time,
        "name": spec.name,
        "sport": sport_of(spec),
        "plan": plan,
        "description": spec.description,
        "workout": workout,
        "spec": spec.model_dump(exclude_none=True),
    }


def build_items(plan: TrainingPlan, time: Optional[str] = None) -> List[dict]:
    items = [feed_item(d, s, time=time, plan=plan.name) for d, s in plan.items]
    seen: dict[str, str] = {}
    for it in items:
        if it["id"] in seen:
            raise ValueError(f"duplicate session: {it['name']!r} twice on {it['date']}")
        seen[it["id"]] = it["date"]
    return items


def preview_plan(plan: TrainingPlan, time: Optional[str] = None) -> List[dict]:
    """Compile (but don't publish) every workout; useful for dry runs/tests."""
    return [
        {"date": it["date"], "name": it["name"], "sport": it["sport"],
         "blocks": len(it["workout"]["blocks"]), "steps": step_count(it["workout"])}
        for it in build_items(plan, time)
    ]


def push_plan(store: FeedStore, plan: TrainingPlan, replace: bool = True,
              time: Optional[str] = None) -> dict:
    """Publish every item to the feed; prune stale items of the same plan."""
    items = build_items(plan, time)
    pruned: list[dict] = []
    if replace:
        keep = {it["id"] for it in items}
        stale = [w["id"] for w in store.load()["workouts"]
                 if w.get("plan") == plan.name and w["id"] not in keep]
        pruned = store.remove(stale)
    stored = store.upsert(items)
    return {
        "published": len(stored),
        "removed_from_previous_push": [{"date": w["date"], "name": w["name"]} for w in pruned],
        "items": [{"date": w["date"], "name": w["name"], "sport": w["sport"], "id": w["id"]}
                  for w in stored],
    }
