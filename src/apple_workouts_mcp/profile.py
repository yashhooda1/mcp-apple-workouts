"""A runner's saved profile: the answers every plan needs, asked once.

Stored as `profile.json` beside the runner's feed (so on a server with
accounts, each person has their own):

    weekly_miles     current weekly running volume
    recent_race      {"distance": "5k", "time": "22:10", "date": "2026-10-03"}
    run_days         the days they run, e.g. ["mon", "tue", "thu", "sat", "sun"]
    quality_days     where the hard sessions go, e.g. ["tue", "fri"]
    long_run_day     e.g. "sat"
    workout_time     usual start time for runs, "06:00"
    strength_time    usual start time for strength, "18:00" (so a lift and a
                     run on the same day don't sit on top of each other)

How it is used:

  * A plan tool called without mileage, days per week, a benchmark or a start
    time takes them from here. An explicit argument always wins.
  * `arrange_week()` re-lays a generated plan onto the runner's days. The
    generators think in a fixed template (hard days Tuesday and Thursday, long
    run Sunday); this maps each template day to the day the runner chose, week
    by week. Strength travels with the day it was paired with, so hard days
    stay hard. The race week is left exactly as generated — it is already
    built backwards from race day.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from .feed import FeedStore, _now
from .fitness import parse_hms

PROFILE_FILE = "profile.json"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RACE_DISTANCES = ("mile", "5k", "10k", "half", "marathon")
BENCHMARK_MAX_AGE_WEEKS = 26
_DAY_ALIASES = {"monday": "mon", "tuesday": "tue", "tues": "tue", "wednesday": "wed",
                "thursday": "thu", "thur": "thu", "thurs": "thu", "friday": "fri",
                "saturday": "sat", "sunday": "sun"}


def day_name(value) -> str:
    """'Saturday' / 'sat' / 5 -> 'sat'."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
        return DAYS[value]
    text = str(value).strip().lower()
    text = _DAY_ALIASES.get(text, text)
    if text not in DAYS:
        raise ValueError(f"unknown day {value!r}; use mon, tue, wed, thu, fri, sat or sun")
    return text


def _clock(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        hh, mm = (int(x) for x in str(value).split(":"))
    except ValueError:
        raise ValueError(f"bad time {value!r}; use 24h 'HH:MM'")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"bad time {value!r}; use 24h 'HH:MM'")
    return f"{hh:02d}:{mm:02d}"


class RecentRace(BaseModel):
    distance: str
    time: str
    date: Optional[str] = None

    @field_validator("distance")
    @classmethod
    def _distance(cls, v):
        v = str(v).strip().lower().replace(" ", "")
        v = {"1mi": "mile", "1mile": "mile", "5km": "5k", "10km": "10k",
             "halfmarathon": "half", "half-marathon": "half", "13.1": "half",
             "26.2": "marathon", "full": "marathon"}.get(v, v)
        if v not in RACE_DISTANCES:
            raise ValueError(f"race distance must be one of {', '.join(RACE_DISTANCES)}")
        return v

    @field_validator("time")
    @classmethod
    def _time(cls, v):
        if parse_hms(str(v)) <= 0:
            raise ValueError("race time must be like '22:10' or '1:38:30'")
        return str(v)

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        return None if v is None else date.fromisoformat(str(v)).isoformat()

    def age_weeks(self, today: Optional[date] = None) -> Optional[int]:
        if not self.date:
            return None
        return ((today or date.today()) - date.fromisoformat(self.date)).days // 7


class RunnerProfile(BaseModel):
    weekly_miles: Optional[float] = None
    recent_race: Optional[RecentRace] = None
    run_days: Optional[list[str]] = None
    quality_days: Optional[list[str]] = None
    long_run_day: Optional[str] = None
    workout_time: Optional[str] = None
    strength_time: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("weekly_miles")
    @classmethod
    def _miles(cls, v):
        if v is not None and not 0 <= v <= 150:
            raise ValueError("weekly_miles must be between 0 and 150")
        return v

    @field_validator("run_days", "quality_days", mode="before")
    @classmethod
    def _days(cls, v):
        if v is None:
            return None
        if isinstance(v, (str, int)):
            v = [v]
        names = {day_name(d) for d in v}
        return [d for d in DAYS if d in names] or None    # week order, no repeats

    @field_validator("long_run_day", mode="before")
    @classmethod
    def _long(cls, v):
        return None if v is None else day_name(v)

    @field_validator("workout_time", "strength_time")
    @classmethod
    def _times(cls, v):
        return _clock(v)

    @model_validator(mode="after")
    def _consistent(self):
        run, quality, long = self.run_days, self.quality_days or [], self.long_run_day
        if run and len(run) < 3:
            raise ValueError("run_days needs at least 3 days")
        if len(quality) > 2:
            raise ValueError("quality_days takes at most 2 days")
        if long and long in quality:
            raise ValueError("the long run day can't also be a quality day")
        if run:
            outside = [d for d in quality + ([long] if long else []) if d not in run]
            if outside:
                raise ValueError(f"{', '.join(outside)} must also be in run_days")
        return self

    # --------------------------------------------------------------- derived
    def benchmark(self, today: Optional[date] = None) -> Optional[tuple[str, str]]:
        """(time, distance) of the recent race if it is fresh enough to set paces."""
        race = self.recent_race
        if not race:
            return None
        age = race.age_weeks(today)
        if age is not None and age > BENCHMARK_MAX_AGE_WEEKS:
            return None
        return race.time, race.distance

    def missing(self) -> list[str]:
        """What a plan would still have to ask for or assume."""
        out = []
        if self.weekly_miles is None:
            out.append("weekly_miles")
        if self.recent_race is None:
            out.append("recent_race")
        if not self.run_days and not self.long_run_day:
            out.append("run_days / long_run_day (default: hard days Tue+Thu, long run Sun)")
        if not self.workout_time:
            out.append("workout_time (default: all-day calendar entries)")
        return out


# ---------------------------------------------------------------------- storage
def _path(store: FeedStore) -> Path:
    return store.dir / PROFILE_FILE


def load(store: FeedStore) -> RunnerProfile:
    path = _path(store)
    if not path.exists():
        return RunnerProfile()
    return RunnerProfile.model_validate(json.loads(path.read_text()))


def save(store: FeedStore, profile: RunnerProfile) -> RunnerProfile:
    profile = profile.model_copy(update={"updated_at": _now()})
    store.dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store.dir, prefix=".tmp-profile-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(profile.model_dump(exclude_none=True), fh, indent=1)
        os.replace(tmp, _path(store))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return profile


def update(store: FeedStore, changes: dict, clear: Optional[list[str]] = None) -> RunnerProfile:
    """Merge `changes` (None values are ignored) and blank the fields in `clear`."""
    current = load(store).model_dump()
    current.update({k: v for k, v in changes.items() if v is not None})
    for name in clear or []:
        if name not in RunnerProfile.model_fields or name == "updated_at":
            raise ValueError(f"unknown profile field {name!r}")
        current[name] = None
    return save(store, RunnerProfile.model_validate(current))   # re-validate the whole


# ------------------------------------------------------------- arranging a week
def week_mapping(layout: dict, run_dows: list[int], profile: RunnerProfile) -> dict[int, int]:
    """Template weekday -> the runner's weekday, for every day the template runs.

    `layout` is the generator's own shape: {"long": 6, "quality": [1, 3]}.
    """
    index = DAYS.index
    template = sorted(set(run_dows))
    t_long = layout["long"]
    t_quality = [q for q in layout.get("quality", []) if q in template]

    p_long = index(profile.long_run_day) if profile.long_run_day else t_long
    wanted = [index(d) for d in (profile.quality_days or [])]
    # As many quality days as the plan has: the runner's first, then the template's.
    p_quality = [d for d in wanted if d != p_long][:len(t_quality)]
    for q in t_quality:
        if len(p_quality) >= len(t_quality):
            break
        if q != p_long and q not in p_quality:
            p_quality.append(q)
    # Still short (the long run took a template quality day): take another of
    # the runner's days, preferring one not next to a hard day.
    candidates = [index(d) for d in profile.run_days] if profile.run_days else list(range(7))
    while len(p_quality) < len(t_quality):
        hard = {p_long, *p_quality}
        free = [d for d in candidates if d not in hard] or \
               [d for d in range(7) if d not in hard]
        apart = [d for d in free if all((d - h) % 7 not in (1, 6) for h in hard)]
        p_quality.append((apart or free)[0])

    mapping = {t_long: p_long, **dict(zip(t_quality, sorted(p_quality)))}
    taken = set(mapping.values())
    easy = [d for d in template if d not in mapping]
    preferred = [index(d) for d in profile.run_days] if profile.run_days else template
    pool = [d for d in preferred if d not in taken]
    pool += [d for d in template if d not in taken and d not in pool]      # too few named
    pool += [d for d in range(7) if d not in taken and d not in pool]      # last resort
    mapping.update(zip(easy, sorted(pool[:len(easy)])))
    return mapping


def _hard_days_note(mapping: dict[int, int], layout: dict) -> Optional[str]:
    hard = sorted({mapping[layout["long"]]}
                  | {mapping[q] for q in layout.get("quality", []) if q in mapping})
    touching = [(a, b) for a in hard for b in hard if (b - a) % 7 == 1]
    if not touching:
        return None
    pairs = ", ".join(f"{DAYS[a]}→{DAYS[b]}" for a, b in touching)
    return (f"Hard days are back to back ({pairs}). That works for some runners, but "
            "an easy or rest day between them is the safer default.")


def arrange_week(plan, profile: RunnerProfile):
    """Re-lay `plan` onto the runner's days. Returns (plan, info or None).

    Plans without a declared layout (hand-written ones, the school track
    block) and profiles with no day preferences come back untouched.
    """
    from .strength import StrengthSpec  # local: avoid a cycle with plans.py

    layout = getattr(plan, "layout", None)
    if not layout or not (profile.run_days or profile.quality_days or profile.long_run_day):
        return plan, None

    def monday_of(iso: str) -> date:
        d = date.fromisoformat(iso)
        return d - timedelta(days=d.weekday())

    race_weeks = {monday_of(d) for d, spec in plan.items if spec.name.startswith("RACE")}
    run_dows = [date.fromisoformat(d).weekday() for d, spec in plan.items
                if not isinstance(spec, StrengthSpec) and monday_of(d) not in race_weeks]
    if not run_dows:
        return plan, None
    mapping = week_mapping(layout, run_dows, profile)
    if all(a == b for a, b in mapping.items()):
        return plan, None

    items = []
    for iso, spec in plan.items:
        monday = monday_of(iso)
        dow = date.fromisoformat(iso).weekday()
        if monday not in race_weeks and dow in mapping:
            iso = (monday + timedelta(days=mapping[dow])).isoformat()
        items.append((iso, spec))
    arranged = type(plan)(name=plan.name, items=sorted(items, key=lambda x: x[0]),
                          family=plan.family, layout=layout)
    info = {"long_run": DAYS[mapping[layout["long"]]],
            "quality": [DAYS[mapping[q]] for q in layout.get("quality", []) if q in mapping],
            "run_days": [DAYS[d] for d in sorted(set(mapping.values()))],
            "race_week": "left as generated"}
    note = _hard_days_note(mapping, layout)
    if note:
        info["note"] = note
    return arranged, info
