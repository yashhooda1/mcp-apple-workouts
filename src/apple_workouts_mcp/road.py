"""Adult road-racing blocks for mile, 5K, 10K and half marathon.

Conservative volume progression; workout pace uses a measured benchmark when
available. Schedules are previews by default at the MCP boundary.

Two tiers, chosen from current weekly mileage:

  * **advanced** (18+ mi/week) — two race-specific quality sessions a week,
    4-7 running days.
  * **base** (5 to under 18 mi/week) — for newer or returning runners: one
    lighter, time-based quality session a week, 3-5 running days, a smaller
    long run, and the same capped weekly ramp. Two hard days a week on that
    little volume is how people get hurt, so the tier isn't optional.
"""
from __future__ import annotations

from datetime import date, timedelta
from math import ceil
from typing import Optional

from .fueling import annotate, weekly_brief
from .plans import TrainingPlan
from .strength import strength_for_week
from .workouts import RepeatSpec, StepSpec, WorkoutSpec

DISTANCES = {"mile": 1.0, "5k": 3.106856, "10k": 6.213712, "half": 13.1094}
BARRIERS = {"mile": "4:59", "5k": "16:59", "10k": "34:59", "half": "1:19:59"}
ALIASES = {"1mi": "mile", "1mile": "mile", "5km": "5k", "10km": "10k", "10000": "10k",
           "half_marathon": "half", "half-marathon": "half", "13.1": "half"}
DAYS = {3: [1, 3, 6], 4: [1, 3, 5, 6], 5: [0, 1, 3, 5, 6],
        6: [0, 1, 2, 3, 5, 6], 7: list(range(7))}
ADVANCED_MIN_MILES = 18.0
BASE_MIN_MILES = 5.0
HALF_MIN_MILES = 10.0
# Long run as a share of the week, and its cap in miles.
LONG_RUN = {"mile": (0.26, 12), "5k": (0.26, 12), "10k": (0.28, 14), "half": (0.30, 16)}
# Tuesday and Thursday are quality days; lifts land after those runs.
EASY = "hr:2"


def seconds(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts):
        raise ValueError("time must be M:SS or H:MM:SS")
    if any(int(part) >= 60 for part in parts[1:]):
        raise ValueError("seconds and minutes fields must be below 60")
    out = 0
    for part in parts:
        out = out * 60 + int(part)
    if out <= 0:
        raise ValueError("time must be positive")
    return out


def clock(total: float) -> str:
    n = int(round(total))
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pace_target(pace_s: float, window: float = 6.0) -> str:
    lo = max(150.0, pace_s - window)
    return f"pace:{clock(lo)}-{clock(pace_s + window)}"


def _run(name: str, miles: float, strides: int = 0, kind: str = "easy") -> WorkoutSpec:
    miles = round(max(2.0, miles), 1)
    steps: list = [StepSpec(kind="run", duration=f"{miles}mi", target=EASY)]
    if strides:
        steps.append(RepeatSpec(repeat=strides, steps=[
            StepSpec(kind="run", duration="20s"),
            StepSpec(kind="recovery", duration="60s", target=EASY),
        ]))
    return WorkoutSpec(
        name=name, steps=steps, estimated_minutes=int(miles * 9),
        description=annotate("Keep easy days conversational.", kind, miles * 9),
    )


def _session(distance: str, week_no: int, race_pace: float, *, secondary: bool,
             taper: bool) -> WorkoutSpec:
    """Two different, race-specific quality sessions per training week."""
    if secondary:
        if distance == "mile":
            reps, length, extra, label = 3, "1mi", 40, "Controlled threshold"
        elif distance == "5k":
            reps, length, extra, label = 3, "1mi", 28, "Threshold cruise"
        elif distance == "10k":
            reps, length, extra, label = 2, "2mi", 18, "10K tempo"
        else:
            reps, length, extra, label = 2, "2mi", 15, "Half-marathon tempo"
        recovery = "2:00"
    else:
        if distance == "mile":
            reps, length, extra, label = 6 + (week_no % 3), "400m", 2, "Mile speed"
        elif distance == "5k":
            reps, length, extra, label = 5 + (week_no % 2), "1km", 5, "5K intervals"
        elif distance == "10k":
            reps, length, extra, label = 4 + (week_no % 2), "1mi", 3, "10K intervals"
        else:
            reps, length, extra, label = 4 + (week_no % 2), "1mi", -8, "HM threshold"
        recovery = "2:00"
    if taper:
        reps = max(2, ceil(reps / 2))
    return WorkoutSpec(
        name=f"{label} {reps}x{length}" + (" (taper)" if taper else ""),
        estimated_minutes=55,
        description=annotate(
            "Finish in control. Jog recoveries slowly; standing rest is a separate "
            "open/rest step if preferred. Adjust for heat and fatigue.",
            "quality", 55, hard=True,
        ),
        steps=[
            StepSpec(kind="warmup", duration="1.5mi", target=EASY),
            RepeatSpec(repeat=reps, steps=[
                StepSpec(kind="run", duration=length,
                         target=pace_target(race_pace + extra)),
                StepSpec(kind="recovery", duration=recovery, target=EASY),
            ]),
            StepSpec(kind="cooldown", duration="1mi", target=EASY),
        ],
    )


def _base_session(distance: str, week_no: int, race_pace: float, *,
                  taper: bool) -> WorkoutSpec:
    """The base tier's one quality day: short, time-based, at goal effort."""
    if distance == "mile":
        reps, length, extra, label = 5 + (week_no % 2), "1:00", 5, "Mile-effort pickups"
    elif distance == "5k":
        reps, length, extra, label = 4 + (week_no % 2), "2:00", 5, "5K-effort repeats"
    elif distance == "10k":
        reps, length, extra, label = 3 + (week_no % 2), "4:00", 5, "10K-effort repeats"
    else:
        reps, length, extra, label = 2 + (week_no % 2), "8:00", 10, "Steady tempo"
    if taper:
        reps = max(2, ceil(reps / 2))
    return WorkoutSpec(
        name=f"{label} {reps}x{length}" + (" (taper)" if taper else ""),
        estimated_minutes=40,
        description=annotate(
            "Comfortably hard, never all-out: you should finish feeling you could do "
            "one more. Walk the recoveries if you need to.",
            "quality", 40, hard=True,
        ),
        steps=[
            StepSpec(kind="warmup", duration="10:00", target=EASY),
            RepeatSpec(repeat=reps, steps=[
                StepSpec(kind="run", duration=length,
                         target=pace_target(race_pace + extra, 10)),
                StepSpec(kind="recovery", duration="2:00", target=EASY),
            ]),
            StepSpec(kind="cooldown", duration="5:00", target=EASY),
        ],
    )


def build_road_plan(
    distance: str,
    goal_time: Optional[str] = None,
    weeks: int = 16,
    start_date: Optional[str] = None,
    race_date: Optional[str] = None,
    current_weekly_miles: float = 30.0,
    days_per_week: int = 5,
    benchmark_time: Optional[str] = None,
    benchmark_distance: Optional[str] = None,
    heat_penalty_s_per_mile: float = 0.0,
    today: Optional[date] = None,
) -> tuple[TrainingPlan, dict]:
    """Build an adult-only mile/5K/10K/half plan, including runner strength.

    The latest measured benchmark anchors *training* intensity. Requested goal
    time is a target, not evidence that the runner can already hold that pace.
    """
    distance = ALIASES.get(distance.strip().lower(), distance.strip().lower())
    if distance not in DISTANCES:
        raise ValueError(
            "distance must be mile, 5k, 10k or half; marathon uses create_marathon_plan")
    if not 8 <= weeks <= 30:
        raise ValueError("weeks must be 8..30")
    if not BASE_MIN_MILES <= current_weekly_miles <= 120:
        raise ValueError(
            "current_weekly_miles must be 5..120; below 5, build to a steady 5 miles "
            "a week of easy running first")
    if distance == "half" and current_weekly_miles < HALF_MIN_MILES:
        raise ValueError(
            "a half-marathon block needs at least 10 current weekly miles; "
            "train for a 5K or 10K first, or build to 10 easy miles a week")
    base_tier = current_weekly_miles < ADVANCED_MIN_MILES
    if base_tier:
        # Fewer days is the point of this tier; the default of 5 is trimmed to
        # what the mileage supports rather than rejected.
        days_per_week = min(days_per_week, 3 if current_weekly_miles < 10 else 4)
    elif days_per_week == 3:
        raise ValueError("days_per_week must be 4..7 at 18+ miles a week")
    if days_per_week not in DAYS:
        raise ValueError("days_per_week must be 3..7")
    if not 0 <= heat_penalty_s_per_mile <= 120:
        raise ValueError("heat penalty must be between 0 and 120 seconds per mile")
    goal_time = goal_time or BARRIERS[distance]
    target_s = seconds(goal_time)
    today = today or date.today()
    monday = date.fromisoformat(start_date) if start_date else (
        today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    )
    if monday.weekday() != 0:
        raise ValueError("start_date must be a Monday")
    race = date.fromisoformat(race_date) if race_date else monday + timedelta(weeks=weeks, days=-1)
    if race < monday + timedelta(weeks=7) or race > monday + timedelta(weeks=30):
        raise ValueError("race_date must be 8..30 calendar weeks from plan start")
    if race_date:
        weeks = (race - monday).days // 7 + 1

    benchmark = None
    if benchmark_time:
        if not benchmark_distance:
            raise ValueError("benchmark_distance is required with benchmark_time")
        key = ALIASES.get(benchmark_distance.strip().lower(), benchmark_distance.strip().lower())
        if key not in DISTANCES:
            raise ValueError("benchmark_distance must be mile, 5k, 10k or half")
        benchmark = seconds(benchmark_time) * (DISTANCES[distance] / DISTANCES[key]) ** 1.06

    # Never assume a goal is present fitness. Without a benchmark we report
    # pace suggestions as aspirational and ask the runner to calibrate them.
    training_s = max(float(target_s), benchmark or float(target_s))
    race_pace = training_s / DISTANCES[distance] + heat_penalty_s_per_mile
    goal_pace = target_s / DISTANCES[distance]
    warning = (
        "No verified recent benchmark: goal-derived quality paces are provisional. "
        "Use controlled effort and replace with a recent race or time trial."
        if benchmark is None else
        ("Benchmark projects slower than the requested goal. Sessions use current "
         "projected fitness, not an imposed goal pace; reassess after 4-6 weeks."
         if benchmark > target_s * 1.02 else
         "Benchmark is near the requested goal; adjust workouts by effort, not watch alerts.")
    )

    items: list[tuple[str, object]] = []
    rows = []
    base = float(current_weekly_miles)
    peak = min(120.0, base * 1.45)
    peak_long = 0.0
    current = base
    for w in range(weeks):
        monday_w = monday + timedelta(weeks=w)
        race_week = monday_w <= race < monday_w + timedelta(days=7)
        taper = w >= weeks - 2
        down = w > 0 and w % 4 == 3
        if race_week:
            current = max(min(10.0, base * 0.6), current * 0.55)
        elif taper:
            current = max(base * 0.65, current * 0.8)
        elif down:
            current = max(base * 0.85, current * 0.78)
        else:
            current = min(peak, current * 1.06)
        target_miles = round(current, 1)
        dow_runs = DAYS[days_per_week]
        share, cap = LONG_RUN[distance]
        # On low volume the long run has to carry more of the week, most of
        # all for the half.
        if base_tier:
            share += 0.12 if distance == "half" else 0.06
        long_miles = max(2.0, round(min(target_miles * share, cap), 1))
        if not race_week:
            peak_long = max(peak_long, long_miles)
        shakeout = 2 if base_tier else 3
        if race_week:
            # The race replaces the week's normal long run.
            dow_runs = [d for d in dow_runs if monday_w + timedelta(days=d) < race
                        and d not in (1, 3, 6)]
            for d in dow_runs:
                items.append(((monday_w + timedelta(days=d)).isoformat(),
                              _run(f"Race-week easy {shakeout}mi + strides", shakeout,
                                   strides=4)))
            if race.weekday() >= 3:
                items.append(((monday_w + timedelta(days=1)).isoformat(),
                              _run(f"Race-week tune-up {shakeout}mi + strides", shakeout,
                                   strides=4)))
            race_spec = WorkoutSpec(
                name=f"RACE — {distance.upper()} ({goal_time})",
                steps=[StepSpec(kind="run", duration=f"{DISTANCES[distance]:.4f}mi",
                                target=pace_target(goal_pace, 10))],
                estimated_minutes=ceil(target_s / 60),
                description=annotate(
                    "Goal pace is a reference, not a command. Race by conditions and readiness.",
                    "race", target_s / 60, hard=True,
                ),
            )
            items.append((race.isoformat(), race_spec))
            # Maintain some mobility, not heavy lifting, during race week.
            if race.weekday() >= 4:
                lift = strength_for_week("taper", race_week=True)[0]
                items.append(((monday_w + timedelta(days=2)).isoformat(), lift))
        else:
            # Reserve about 2x~5mi for quality days; split remaining miles
            # between easy days. This is an approximate weekly target, not a
            # strict sum: warmups/interval distances contribute as well.
            # The base tier has one quality day (Tuesday); Thursday is easy.
            hard_days = (1, 6) if base_tier else (1, 3, 6)
            easy_days = [d for d in dow_runs if d not in hard_days]
            quality_allowance = 3.0 if base_tier else 11.0
            easy_miles = max(2.0, (target_miles - quality_allowance - long_miles)
                             / max(1, len(easy_days)))
            for d in dow_runs:
                when = (monday_w + timedelta(days=d)).isoformat()
                if d == 1 and base_tier:
                    spec = _base_session(distance, w, race_pace, taper=taper)
                elif d == 1:
                    spec = _session(distance, w, race_pace, secondary=False, taper=taper)
                elif d == 3 and not base_tier:
                    spec = _session(distance, w, race_pace, secondary=True, taper=taper)
                elif d == 6:
                    spec = _run(f"Long easy {long_miles}mi", long_miles, kind="long")
                else:
                    spec = _run(f"Easy {easy_miles:.1f}mi", easy_miles,
                                strides=4 if d == 0 and not down else 0)
                items.append((when, spec))
            lifts = strength_for_week("taper" if taper else "build", down_week=down)
            for d, lift in zip((1, 3), lifts):
                items.append(((monday_w + timedelta(days=d)).isoformat(), lift))
        rows.append({
            "week": w + 1, "starting": monday_w.isoformat(), "phase":
                "race" if race_week else "taper" if taper else "down" if down else "build",
            "approx_target_miles": target_miles,
            "fueling": weekly_brief("taper" if taper else "build", target_miles,
                                    race_week=race_week, down_week=down),
        })
    name = f"{distance.upper()} — {goal_time} build to {race.isoformat()}"
    plan = TrainingPlan(name=name, items=sorted(items, key=lambda x: x[0]),
                        family=distance.upper())
    report = {
        "race": {"distance": distance, "date": race.isoformat()},
        "goal_time": goal_time, "weeks": weeks, "days_per_week": days_per_week,
        "tier": "base" if base_tier else "advanced",
        "longest_run_miles": peak_long,
        "long_run_note": (
            f"The longest run in this block is {peak_long:g} miles, well short of "
            f"the {DISTANCES[distance]:.1f}-mile race. Plan run/walk on race day, or "
            "start from more weekly miles or more weeks."
            if peak_long < 0.7 * DISTANCES[distance] else None),
        "training_pace_per_mile": clock(race_pace),
        "goal_pace_per_mile": clock(goal_pace),
        "benchmark_projection": clock(benchmark) if benchmark is not None else None,
        "warning": warning,
        "weekly_plan": rows,
        "safety": ("Adult plan for newer or returning runners: one quality day a "
                   "week, the rest easy. Walk breaks are fine. " if base_tier else
                   "Adult advanced plan only. ") +
                  "Modify or stop for persistent pain, "
                  "illness, insufficient recovery or unusual fatigue. Strength is "
                  "supplementary; keep hard days hard and easy days easy.",
    }
    return plan, report
