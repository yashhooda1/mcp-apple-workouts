"""Adult road-racing blocks for mile, 5K and half marathon.

Conservative volume progression; workout pace uses a measured benchmark when
available. Schedules are previews by default at the MCP boundary.
"""
from __future__ import annotations

from datetime import date, timedelta
from math import ceil
from typing import Optional

from .fueling import annotate, weekly_brief
from .plans import TrainingPlan
from .strength import strength_for_week
from .workouts import RepeatSpec, StepSpec, WorkoutSpec

DISTANCES = {"mile": 1.0, "5k": 3.106856, "half": 13.1094}
BARRIERS = {"mile": "4:59", "5k": "16:59", "half": "1:19:59"}
ALIASES = {"1mi": "mile", "1mile": "mile", "5km": "5k",
           "half_marathon": "half", "half-marathon": "half", "13.1": "half"}
DAYS = {4: [1, 3, 5, 6], 5: [0, 1, 3, 5, 6],
        6: [0, 1, 2, 3, 5, 6], 7: list(range(7))}
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
        else:
            reps, length, extra, label = 2, "2mi", 15, "Half-marathon tempo"
        recovery = "2:00"
    else:
        if distance == "mile":
            reps, length, extra, label = 6 + (week_no % 3), "400m", 2, "Mile speed"
        elif distance == "5k":
            reps, length, extra, label = 5 + (week_no % 2), "1km", 5, "5K intervals"
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
    """Build an adult-only mile/5K/half plan, including runner strength.

    The latest measured benchmark anchors *training* intensity. Requested goal
    time is a target, not evidence that the runner can already hold that pace.
    """
    distance = ALIASES.get(distance.strip().lower(), distance.strip().lower())
    if distance not in DISTANCES:
        raise ValueError("distance must be mile, 5k or half; marathon uses create_marathon_plan")
    if not 8 <= weeks <= 30:
        raise ValueError("weeks must be 8..30")
    if days_per_week not in DAYS:
        raise ValueError("days_per_week must be 4..7")
    if not 18 <= current_weekly_miles <= 120:
        raise ValueError("current_weekly_miles must be 18..120 for this advanced adult plan")
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
            raise ValueError("benchmark_distance must be mile, 5k or half")
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
    current = base
    for w in range(weeks):
        monday_w = monday + timedelta(weeks=w)
        race_week = monday_w <= race < monday_w + timedelta(days=7)
        taper = w >= weeks - 2
        down = w > 0 and w % 4 == 3
        if race_week:
            current = max(10.0, current * 0.55)
        elif taper:
            current = max(base * 0.65, current * 0.8)
        elif down:
            current = max(base * 0.85, current * 0.78)
        else:
            current = min(peak, current * 1.06)
        target_miles = round(current, 1)
        dow_runs = DAYS[days_per_week]
        long_miles = round(min(target_miles * (0.30 if distance == "half" else 0.26),
                               16 if distance == "half" else 12), 1)
        if race_week:
            # The race replaces the week's normal long run.
            dow_runs = [d for d in dow_runs if monday_w + timedelta(days=d) < race
                        and d not in (1, 3, 6)]
            for d in dow_runs:
                items.append(((monday_w + timedelta(days=d)).isoformat(),
                              _run("Race-week easy 3mi + strides", 3, strides=4)))
            if race.weekday() >= 3:
                items.append(((monday_w + timedelta(days=1)).isoformat(),
                              _run("Race-week tune-up 3mi + strides", 3, strides=4)))
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
            easy_days = [d for d in dow_runs if d not in (1, 3, 6)]
            quality_allowance = 11.0
            easy_miles = max(2.0, (target_miles - quality_allowance - long_miles)
                             / max(1, len(easy_days)))
            for d in dow_runs:
                when = (monday_w + timedelta(days=d)).isoformat()
                if d == 1:
                    spec = _session(distance, w, race_pace, secondary=False, taper=taper)
                elif d == 3:
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
    plan = TrainingPlan(name=name, items=sorted(items, key=lambda x: x[0]))
    report = {
        "race": {"distance": distance, "date": race.isoformat()},
        "goal_time": goal_time, "weeks": weeks, "days_per_week": days_per_week,
        "training_pace_per_mile": clock(race_pace),
        "goal_pace_per_mile": clock(goal_pace),
        "benchmark_projection": clock(benchmark) if benchmark is not None else None,
        "warning": warning,
        "weekly_plan": rows,
        "safety": "Adult advanced plan only. Modify or stop for persistent pain, "
                  "illness, insufficient recovery or unusual fatigue. Strength is "
                  "supplementary; keep hard days hard and easy days easy.",
    }
    return plan, report
