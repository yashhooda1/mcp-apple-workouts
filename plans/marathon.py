"""Adaptive marathon generator: any race, any date, any starting fitness.

The Houston block in `plans/houston.py` is a hand-written block for one athlete
and one race. This module is the general case — point it at a race and a goal
and it works backwards:

    1. Read the athlete's real training (Strava, or a mileage you supply) (`fitness.summarize_fitness`).
    2. Compare that to what the goal asks for (`fitness.assess_gap`), and say so
       out loud when the gap doesn't fit in the weeks available.
    3. Build a mileage ramp that starts where the athlete actually is — 8%/week
       ceiling, 3-up-1-down, three-week taper.
    4. Fill each week with phase-appropriate sessions, paced off *current*
       fitness early and *goal* fitness late.
    5. Schedule two strength sessions every single week.
    6. Attach fuelling and recovery guidance to each session's description, so
       it syncs to the watch with the workout.

The gap analysis is the point. Somebody running 18 miles a week who wants a 3:15
in 14 weeks gets a plan built to what 14 weeks can actually deliver, plus a plain
statement of what the original goal would have needed. Building the fantasy plan
instead is how runners end up injured in week 9.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from apple_workouts_mcp.fitness import (
    MI_MARATHON,
    FitnessSnapshot,
    GapAssessment,
    assess_gap,
    fmt_hms,
    fmt_pace,
    mileage_ramp,
    paces_from_marathon,
    paces_from_race,
)
from apple_workouts_mcp.fueling import annotate, weekly_brief
from apple_workouts_mcp.plans import TrainingPlan
from apple_workouts_mcp.strength import strength_for_week
from apple_workouts_mcp.workouts import RepeatSpec, StepSpec, WorkoutSpec

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

EASY = "hr:2"

# Weekday templates. Strength always lands on a quality day so easy days stay
# easy, and never the day before the long run.
DAY_TEMPLATES = {
    4: {"quality": TUE, "medium": THU, "long": SUN, "easy": [SAT],
        "strength": [TUE, THU]},
    5: {"quality": TUE, "medium": THU, "long": SUN, "easy": [MON, SAT],
        "strength": [TUE, THU]},
    6: {"quality": TUE, "medium": THU, "long": SUN, "easy": [MON, WED, SAT],
        "strength": [TUE, THU]},
    7: {"quality": TUE, "medium": THU, "long": SUN, "easy": [MON, WED, FRI, SAT],
        "strength": [TUE, THU]},
}


# --------------------------------------------------------------------------- #
# session builders
# --------------------------------------------------------------------------- #
def _minutes(miles: float, pace_s: float) -> int:
    return max(15, int(miles * pace_s / 60))


def easy_run(miles: float, pace_s: float, strides: int = 0, level: str = "adult",
             name: Optional[str] = None) -> WorkoutSpec:
    miles = round(max(2.0, miles), 1)
    steps: list = [StepSpec(kind="run", duration=f"{miles}mi", target=EASY)]
    if strides:
        steps.append(RepeatSpec(repeat=strides, steps=[
            StepSpec(kind="run", duration="20s", target=None),
            StepSpec(kind="recovery", duration="60s", target=EASY),
        ]))
    label = name or (f"Easy {miles}mi" + (f" + {strides}x20s strides" if strides else ""))
    mins = _minutes(miles, pace_s)
    return WorkoutSpec(name=label, steps=steps, estimated_minutes=mins,
                       description=annotate(None, "easy", mins, level, hard=False))


def intervals(name: str, reps: int, work: str, rec: str, target: str,
              wu: float, cd: float, pace_s: float, level: str = "adult") -> WorkoutSpec:
    mins = _minutes(wu + cd + reps * 1.3, pace_s)
    return WorkoutSpec(
        name=name,
        estimated_minutes=mins,
        description=annotate(None, "quality", mins, level, hard=True),
        steps=[
            StepSpec(kind="warmup", duration=f"{wu}mi", target=EASY),
            RepeatSpec(repeat=reps, steps=[
                StepSpec(kind="run", duration=work, target=target),
                StepSpec(kind="recovery", duration=rec, target=EASY),
            ]),
            StepSpec(kind="cooldown", duration=f"{cd}mi", target=EASY),
        ],
    )


def tempo(name: str, blocks: list[tuple[str, str]], wu: float, cd: float,
          pace_s: float, level: str = "adult", float_rec: str = "1mi") -> WorkoutSpec:
    steps: list = [StepSpec(kind="warmup", duration=f"{wu}mi", target=EASY)]
    for i, (dur, tgt) in enumerate(blocks):
        if i:
            steps.append(StepSpec(kind="recovery", duration=float_rec, target=EASY))
        steps.append(StepSpec(kind="run", duration=dur, target=tgt))
    steps.append(StepSpec(kind="cooldown", duration=f"{cd}mi", target=EASY))
    mins = _minutes(wu + cd + 2 * len(blocks), pace_s)
    return WorkoutSpec(name=name, steps=steps, estimated_minutes=mins,
                       description=annotate(None, "quality", mins, level, hard=True))


def long_run(miles: float, pace_s: float, finish_miles: float = 0,
             finish_target: str = "", finish_label: str = "steady",
             level: str = "adult") -> WorkoutSpec:
    miles = round(max(6.0, miles), 1)
    finish_miles = round(min(finish_miles, miles - 4), 1) if finish_miles else 0
    steps = [StepSpec(kind="run", duration=f"{round(miles - finish_miles, 1)}mi",
                      target=EASY)]
    label = f"Long {miles}mi"
    if finish_miles > 0:
        steps.append(StepSpec(kind="run", duration=f"{finish_miles}mi",
                              target=finish_target))
        label += f" w/ last {finish_miles} @ {finish_label}"
    mins = _minutes(miles, pace_s)
    return WorkoutSpec(name=label, steps=steps, estimated_minutes=mins,
                       description=annotate(None, "long", mins, level, hard=True))


def long_with_mp(miles: float, reps: int, work: str, rec: str, mp_target: str,
                 pace_s: float, level: str = "adult") -> WorkoutSpec:
    miles = round(miles, 1)
    mins = _minutes(miles, pace_s)
    return WorkoutSpec(
        name=f"Long {miles}mi w/ {reps}x{work} @ MP",
        estimated_minutes=mins,
        description=annotate(None, "long", mins, level, hard=True),
        steps=[
            StepSpec(kind="warmup", duration="3mi", target=EASY),
            RepeatSpec(repeat=reps, steps=[
                StepSpec(kind="run", duration=work, target=mp_target),
                StepSpec(kind="recovery", duration=rec, target=EASY),
            ]),
            StepSpec(kind="cooldown", duration="2mi", target=EASY),
        ],
    )


def race_day(race_name: str, goal_seconds: float, level: str = "adult") -> WorkoutSpec:
    mp = goal_seconds / MI_MARATHON
    start = f"pace:{fmt_pace(mp + 4)}-{fmt_pace(mp + 12)}"
    middle = f"pace:{fmt_pace(mp - 3)}-{fmt_pace(mp + 4)}"
    close = f"pace:{fmt_pace(mp - 10)}-{fmt_pace(mp + 2)}"
    return WorkoutSpec(
        name=f"RACE — {race_name} ({fmt_hms(goal_seconds)})",
        estimated_minutes=int(goal_seconds / 60) + 5,
        description=annotate(
            f"Goal {fmt_hms(goal_seconds)} = {fmt_pace(mp)}/mi. First 10K deliberately "
            f"slower than goal pace — you cannot win the race there, only lose it.",
            "race", goal_seconds / 60, level, hard=True),
        steps=[
            StepSpec(kind="run", duration="6mi", target=start),
            StepSpec(kind="run", duration="14mi", target=middle),
            StepSpec(kind="run", duration="6.2mi", target=close),
        ],
    )


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
def phase_map(weeks: int) -> list[str]:
    """Split the block into base / build / specific / taper.

    Short blocks give up base first, then build — the specific phase and the
    taper are the two things never sacrificed.
    """
    taper = 3
    body = max(1, weeks - taper)
    if body <= 5:
        phases = ["specific"] * body
    elif body <= 9:
        specific = max(4, body // 2)
        phases = ["build"] * (body - specific) + ["specific"] * specific
    else:
        base = max(2, int(body * 0.30))
        specific = max(5, int(body * 0.33))
        build = body - base - specific
        phases = ["base"] * base + ["build"] * build + ["specific"] * specific
    return phases + ["taper"] * taper


def _week_sessions(phase: str, miles: float, week_in_phase: int, t: dict,
                   days: dict, level: str, down: bool,
                   long_cap: float) -> dict[int, WorkoutSpec]:
    """One week of running, as {weekday: WorkoutSpec}."""
    easy_pace = 9.5 * 60 if phase == "base" else 9.0 * 60  # only used for time estimates
    long_share = 0.26 if phase == "base" else 0.30
    long_miles = min(round(miles * long_share, 1), long_cap)
    quality_miles = round(miles * 0.19, 1)
    medium_miles = round(miles * 0.17, 1)
    remaining = max(0.0, miles - long_miles - quality_miles - medium_miles)
    easy_days = days["easy"]
    per_easy = round(remaining / max(1, len(easy_days)), 1)

    out: dict[int, WorkoutSpec] = {}

    # --- quality session ---------------------------------------------------
    wu = cd = 2.0
    if phase == "base":
        session = intervals(f"{4 + week_in_phase % 3}x2:00 hill/pace strides",
                            4 + week_in_phase % 3, "2:00", "2:00", t["FIVE_K"],
                            wu, cd, easy_pace, level)
    elif phase == "build":
        reps = 4 + week_in_phase % 3
        session = intervals(f"{reps}x1mi @ threshold", reps, "1mi", "2:00",
                            t["THRESH"], wu, cd, easy_pace, level)
    elif phase == "specific":
        blocks = [("4mi", t["MP"]), ("4mi", t["MP"])] if week_in_phase % 2 \
            else [("3mi", t["THRESH"]), ("3mi", t["THRESH"])]
        session = tempo("2x4mi @ MP" if week_in_phase % 2 else "2x3mi @ threshold",
                        blocks, wu, cd, easy_pace, level)
    else:  # taper
        reps = max(3, 5 - week_in_phase)
        session = intervals(f"{reps}x1mi @ MP — tune-up", reps, "1mi", "2:00",
                            t["MP"], 2.0, 1.5, easy_pace, level)
    out[days["quality"]] = session

    # --- medium / second quality -------------------------------------------
    if phase in ("base", "taper"):
        out[days["medium"]] = easy_run(medium_miles, easy_pace, strides=6, level=level)
    elif phase == "build":
        out[days["medium"]] = easy_run(medium_miles, easy_pace, strides=8, level=level)
    else:
        out[days["medium"]] = long_run(medium_miles, easy_pace,
                                       finish_miles=min(4, medium_miles / 2),
                                       finish_target=t["MP"], finish_label="MP",
                                       level=level)

    # --- long run -----------------------------------------------------------
    if phase == "base":
        out[days["long"]] = long_run(long_miles, easy_pace, level=level)
    elif phase == "build":
        out[days["long"]] = long_run(long_miles, easy_pace,
                                     finish_miles=min(5, long_miles / 4),
                                     finish_target=t["STEADY"], finish_label="steady",
                                     level=level)
    elif phase == "specific":
        if week_in_phase % 2 == 0 and long_miles >= 16:
            out[days["long"]] = long_with_mp(long_miles, 2, "4mi", "1mi", t["MP"],
                                             easy_pace, level)
        else:
            out[days["long"]] = long_run(long_miles, easy_pace,
                                         finish_miles=min(6, long_miles / 3),
                                         finish_target=t["MP"], finish_label="MP",
                                         level=level)
    else:
        out[days["long"]] = long_run(long_miles, easy_pace, level=level)

    # --- easy days ----------------------------------------------------------
    for i, dow in enumerate(easy_days):
        if per_easy < 2:
            continue
        out[dow] = easy_run(per_easy, easy_pace, strides=6 if i == 0 and not down else 0,
                            level=level)
    return out


# --------------------------------------------------------------------------- #
# the generator
# --------------------------------------------------------------------------- #
def build_marathon_plan(
    race_name: str,
    race_date: str,
    goal_time: str,
    snapshot: Optional[FitnessSnapshot] = None,
    current_weekly_miles: Optional[float] = None,
    days_per_week: int = 5,
    start_date: Optional[str] = None,
    heat_penalty: float = 0.0,
    level: str = "adult",
    force_goal: bool = False,
    today: Optional[date] = None,
) -> tuple[TrainingPlan, dict]:
    """Build a full marathon block ending on race day.

    Returns `(plan, report)`. The report carries the gap assessment, the weekly
    mileage ramp, the derived paces, and the weekly nutrition/recovery briefs —
    everything the assistant should tell the athlete but that doesn't belong on
    a watch.
    """
    today = today or date.today()
    snapshot = snapshot or FitnessSnapshot(weeks_analyzed=0)
    race = date.fromisoformat(race_date)

    assessment: GapAssessment = assess_gap(
        snapshot, goal_time, race_date, today=today,
        current_weekly_miles=current_weekly_miles,
    )
    build_goal_s = assessment.goal_seconds if force_goal \
        else assessment.recommended_goal_seconds

    # Start on the Monday of the first full week, end on race day.
    if start_date:
        monday = date.fromisoformat(start_date)
    else:
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    if monday.weekday() != 0:
        raise ValueError(f"start_date must be a Monday, got {monday} ({monday:%A})")

    weeks = max(4, (race - monday).days // 7 + 1)
    phases = phase_map(weeks)
    current = current_weekly_miles if current_weekly_miles is not None \
        else max(snapshot.avg_weekly_miles, 12.0)
    ramp = mileage_ramp(current, min(assessment.achievable_peak_mpw,
                                     assessment.required_peak_mpw), weeks)

    # Paces: goal paces for race-specific work; if we have a measured effort, the
    # early phases run off *that* instead, so week 1 isn't paced at week-20 fitness.
    goal_paces = paces_from_marathon(build_goal_s)
    current_paces = (
        paces_from_race(snapshot.best_effort["time_s"], snapshot.best_effort["distance_mi"])
        if snapshot.best_effort else goal_paces
    )

    days = DAY_TEMPLATES.get(days_per_week, DAY_TEMPLATES[5])
    long_cap = 22.0 if build_goal_s <= 3 * 3600 + 30 * 60 else 20.0

    items: list[tuple[str, object]] = []
    week_rows: list[dict] = []
    phase_counter: dict[str, int] = {}

    for w in range(weeks):
        phase = phases[min(w, len(phases) - 1)]
        phase_counter[phase] = phase_counter.get(phase, 0) + 1
        week_in_phase = phase_counter[phase]
        monday_w = monday + timedelta(days=7 * w)
        is_race_week = (race - monday_w).days < 7
        down = (not is_race_week) and phase != "taper" and week_in_phase % 4 == 0
        miles = ramp[min(w, len(ramp) - 1)]

        paces = current_paces if phase in ("base", "build") else goal_paces
        targets = paces.targets(heat_penalty if phase in ("base", "build") else 0.0)

        if is_race_week:
            sessions = {
                MON: easy_run(max(3, miles * 0.12), 9 * 60, level=level),
                TUE: intervals("3x1mi @ MP — final tune", 3, "1mi", "2:00",
                               targets["MP"], 2.0, 1.0, 9 * 60, level),
                THU: easy_run(3, 9 * 60, level=level, name="Shakeout 3mi"),
                SAT: easy_run(2, 9 * 60, strides=4, level=level,
                              name="Pre-race 2mi + strides"),
            }
            sessions = {d: s for d, s in sessions.items()
                        if monday_w + timedelta(days=d) < race}
            sessions[race.weekday()] = race_day(race_name, build_goal_s, level)
        else:
            sessions = _week_sessions(phase, miles, week_in_phase, targets, days,
                                      level, down, long_cap)

        for dow, spec in sorted(sessions.items()):
            items.append(((monday_w + timedelta(days=dow)).isoformat(), spec))

        # Two strength sessions. Every week. No exceptions.
        lifts = strength_for_week(phase, down_week=down, race_week=is_race_week,
                                  level=level)
        for dow, lift in zip(days["strength"], lifts):
            when = monday_w + timedelta(days=dow)
            if when < race:
                items.append((when.isoformat(), lift))

        week_rows.append({
            "week": w + 1,
            "starting": monday_w.isoformat(),
            "phase": phase,
            "target_miles": miles,
            "down_week": down,
            "race_week": is_race_week,
            "strength_sessions": [s.name for s in lifts],
            "brief": weekly_brief(phase, miles, level, down_week=down,
                                  race_week=is_race_week),
        })

    plan = TrainingPlan(
        name=f"{race_name} — {fmt_hms(build_goal_s)} build",
        items=sorted(items, key=lambda it: it[0]),
    )
    report = {
        "race": {"name": race_name, "date": race.isoformat()},
        "goal_requested": fmt_hms(assessment.goal_seconds),
        "goal_built_toward": fmt_hms(build_goal_s),
        "forced": force_goal,
        "weeks": weeks,
        "assessment": assessment.as_dict(),
        "fitness": snapshot.as_dict(),
        "paces": {
            "goal": {k: v for k, v in goal_paces.targets().items()},
            "current_fitness": {k: v for k, v in current_paces.targets().items()},
            "heat_penalty_s_per_mi": heat_penalty,
        },
        "mileage_ramp": ramp,
        "weeks_detail": week_rows,
        "sessions": len(items),
    }
    return plan, report
