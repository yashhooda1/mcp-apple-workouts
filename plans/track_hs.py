"""High-school track blocks built toward four classic barrier times:

    800m   sub-2:00
    1600m  sub-4:30
    3200m  sub-10:00
    5K     sub-15:00      (cross-country / road)

Any goal time can be passed; those four are the presets because they are the
marks a high-school distance runner actually organises a season around.

Guardrails, because the athletes here are minors
------------------------------------------------
This module is deliberately more conservative than the marathon generator:

  * **Hard mileage ceilings by event and training age** (`MILEAGE_CEILING`).
    The plan will refuse to ramp past them no matter what goal is requested.
  * **A mandatory full rest day every week.** Sunday is never scheduled.
  * **No doubles, ever.** Two-a-days are a college decision, not a sophomore one.
  * **Quality is capped at two sessions a week**, plus a meet — three hard days
    in a school week is how teenagers get hurt.
  * **Volume holds, never climbs, through a down week or a growth spurt.**
  * Nutrition guidance runs through `fueling` at `level="hs"`, which emits
    adequacy-only advice: eat enough, eat often, no calorie or weight targets.
    Under-fuelling is the most common serious mistake in high-school distance
    running, and this tool will not contribute to it.

This is a template, not a coach. A high-school athlete has an actual coach with
an actual meet schedule and an actual view of that athlete's history — this plan
is something to bring to that coach, not something to run instead of them. Any
pain that changes how you run, or bone pain that hurts when hopping on one leg,
is a stop-and-see-someone signal.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from apple_workouts_mcp.fitness import (
    MI_5K,
    MI_800,
    MI_1600,
    MI_3200,
    fmt_hms,
    fmt_pace,
    parse_hms,
    riegel,
    window,
)
from apple_workouts_mcp.fueling import annotate, weekly_brief
from apple_workouts_mcp.plans import TrainingPlan
from apple_workouts_mcp.strength import strength_for_week
from apple_workouts_mcp.workouts import RepeatSpec, StepSpec, WorkoutSpec

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
EASY = "hr:2"

EVENT_MI = {"800": MI_800, "1600": MI_1600, "3200": MI_3200, "5k": MI_5K}

PRESET_GOALS = {
    "800": "2:00",
    "1600": "4:30",
    "3200": "10:00",
    "5k": "15:00",
}

# Weekly mileage ceilings. Keyed by event, then training age:
# 'new' = first or second season running, 'developing' = 2-3 seasons,
# 'experienced' = 3+ seasons of consistent year-round running.
MILEAGE_CEILING = {
    "800": {"new": 22, "developing": 30, "experienced": 38},
    "1600": {"new": 25, "developing": 35, "experienced": 45},
    "3200": {"new": 28, "developing": 40, "experienced": 50},
    "5k": {"new": 30, "developing": 42, "experienced": 55},
}

# How much a high schooler can realistically take off a PR in one block, as a
# fraction of current time. Low training age improves fastest; an experienced
# senior is closer to their ceiling.
IMPROVEMENT_RATE = {"new": 0.055, "developing": 0.035, "experienced": 0.020}

MAX_WEEKLY_RAMP = 1.07


# --------------------------------------------------------------------------- #
# paces
# --------------------------------------------------------------------------- #
def event_paces(event: str, goal_time: str,
                aerobic_reference: Optional[tuple[str, str]] = None) -> dict:
    """Pace windows (sec/mile) for a track block.

    Race-specific reps are paced off the **goal**. Aerobic work — threshold,
    long runs — is paced off `aerobic_reference`, a (event, time) pair for a
    real current result, when one is given. Extrapolating a 5K pace from an 800
    goal over-predicts endurance badly, so when no reference exists the module
    uses a steeper Riegel exponent and says so in the report.
    """
    goal_s = parse_hms(goal_time)
    dist = EVENT_MI[event]
    # Steeper than 1.06 when extrapolating upward from a short event.
    up_exp = 1.09 if event in ("800", "1600") else 1.06

    def equiv(target_mi: float) -> float:
        exp = up_exp if target_mi > dist else 1.06
        return riegel(goal_s, dist, target_mi, exp) / target_mi

    if aerobic_reference:
        ref_event, ref_time = aerobic_reference
        ref_s, ref_mi = parse_hms(ref_time), EVENT_MI[ref_event]
        five_k = riegel(ref_s, ref_mi, MI_5K, 1.06) / MI_5K
    else:
        five_k = equiv(MI_5K)

    return {
        "RACE": window(goal_s / dist, 3, 3),
        "P800": window(equiv(MI_800), 3, 3),
        "P1600": window(equiv(MI_1600), 4, 4),
        "P3200": window(equiv(MI_3200), 5, 5),
        "P5K": window(five_k, 6, 6),
        "THRESH": window(five_k + 28, 7, 7),
        "REP": window(equiv(MI_800) - 8, 4, 4),   # 200/300 speed, faster than 800 pace
        "EASY": EASY,
        "_race_pace_s": goal_s / dist,
        "_five_k_s": five_k,
        "_aerobic_from_reference": bool(aerobic_reference),
    }


def splits(event: str, goal_time: str) -> dict:
    """Goal splits an athlete can actually repeat back on the track."""
    goal_s = parse_hms(goal_time)
    per_mi = goal_s / EVENT_MI[event]
    per_400 = per_mi * (400 / 1609.34)
    return {
        "goal": fmt_hms(goal_s),
        "per_400": f"{per_400:.1f}s",
        "per_200": f"{per_400 / 2:.1f}s",
        "per_mile": f"{fmt_pace(per_mi)}/mi",
    }


# --------------------------------------------------------------------------- #
# gap analysis
# --------------------------------------------------------------------------- #
def assess_track_gap(event: str, goal_time: str, weeks: int,
                     current_pr: Optional[str] = None,
                     current_weekly_miles: float = 0.0,
                     training_age: str = "developing") -> dict:
    """What the goal asks for versus what this block can deliver."""
    goal_s = parse_hms(goal_time)
    ceiling = MILEAGE_CEILING[event][training_age]
    rate = IMPROVEMENT_RATE[training_age] * min(1.0, weeks / 16)

    gaps: list[str] = []
    actions: list[str] = []
    verdict = "on_track"
    projected_s = None

    if current_pr:
        pr_s = parse_hms(current_pr)
        projected_s = pr_s * (1 - rate)
        needed = (pr_s - goal_s) / pr_s
        if goal_s >= pr_s:
            verdict = "already_achieved"
            actions.append(
                f"Current PR of {fmt_hms(pr_s)} is already inside {fmt_hms(goal_s)} — "
                "set the next barrier and re-run this."
            )
        elif needed <= rate:
            verdict = "on_track"
            actions.append(
                f"{needed * 100:.1f}% off the PR over {weeks} weeks is inside what a "
                f"{training_age} athlete typically gains in a block."
            )
        elif needed <= rate * 1.6:
            verdict = "stretch"
            gaps.append(
                f"The goal needs {needed * 100:.1f}% off {fmt_hms(pr_s)}; a typical block "
                f"at this training age returns about {rate * 100:.1f}%. Reachable on a "
                "good day with a good race, not a given."
            )
            actions.append(
                f"Plan targets {fmt_hms(goal_s)} as the A-goal with {fmt_hms(projected_s)} "
                "as the realistic checkpoint. Race-pace reps are set at the A-goal."
            )
        else:
            verdict = "multi_season"
            gaps.append(
                f"{fmt_hms(goal_s)} is {needed * 100:.1f}% faster than {fmt_hms(pr_s)}. "
                f"That is normally a two-or-three-season project, not a {weeks}-week one."
            )
            actions.append(
                f"This block builds toward {fmt_hms(projected_s)} — a real, big PR — and "
                f"sets up {fmt_hms(goal_s)} for next season. Chasing the bigger number now "
                "mostly buys injury risk."
            )
    else:
        gaps.append("No current PR supplied, so the plan can't size the gap. "
                    "Race-pace work is set at the goal and aerobic work starts "
                    "conservatively.")

    if current_weekly_miles and current_weekly_miles > ceiling:
        gaps.append(
            f"Current {current_weekly_miles:.0f} mi/wk is already above this module's "
            f"ceiling of {ceiling} for a {training_age} {event} runner. The plan holds "
            "volume flat and adds quality instead."
        )
    elif current_weekly_miles:
        headroom = ceiling - current_weekly_miles
        if headroom > 0:
            actions.append(
                f"Room to build from {current_weekly_miles:.0f} to about {ceiling} mi/wk "
                "at 7%/week, with a down week every fourth week."
            )

    actions.append(
        "Two strength sessions a week are built in. For a high schooler this is bone "
        "and tendon insurance — it matters more than any single workout in the block."
    )
    actions.append(
        "Bring this to your actual coach before you run it. They have your meet "
        "schedule and your history; this file has neither."
    )

    return {
        "event": event,
        "goal": fmt_hms(goal_s),
        "goal_splits": splits(event, goal_time),
        "current_pr": fmt_hms(parse_hms(current_pr)) if current_pr else None,
        "weeks": weeks,
        "training_age": training_age,
        "weekly_mileage_ceiling": ceiling,
        "realistic_projection": fmt_hms(projected_s) if projected_s else None,
        "verdict": verdict,
        "gaps": gaps,
        "actions": actions,
    }


# --------------------------------------------------------------------------- #
# session builders
# --------------------------------------------------------------------------- #
def _easy(miles: float, strides: int = 0, name: Optional[str] = None) -> WorkoutSpec:
    miles = round(max(2.0, miles), 1)
    steps: list = [StepSpec(kind="run", duration=f"{miles}mi", target=EASY)]
    if strides:
        steps.append(RepeatSpec(repeat=strides, steps=[
            StepSpec(kind="run", duration="20s", target=None),
            StepSpec(kind="recovery", duration="60s", target=EASY),
        ]))
    label = name or f"Easy {miles}mi" + (f" + {strides}x20s strides" if strides else "")
    mins = int(miles * 8.5)
    return WorkoutSpec(name=label, steps=steps, estimated_minutes=mins,
                       description=annotate(None, "easy", mins, "hs", hard=False))


def _track(name: str, reps: int, work: str, rec: str, target: str,
           wu: float = 1.5, cd: float = 1.5, sets: int = 1,
           set_rest: str = "4:00") -> WorkoutSpec:
    body: list = []
    for s in range(sets):
        if s:
            body.append(StepSpec(kind="recovery", duration=set_rest, target=EASY))
        body.append(RepeatSpec(repeat=reps, steps=[
            StepSpec(kind="run", duration=work, target=target),
            StepSpec(kind="recovery", duration=rec, target=EASY),
        ]))
    mins = int((wu + cd) * 8.5 + reps * sets * 3)
    return WorkoutSpec(
        name=name,
        estimated_minutes=mins,
        description=annotate(None, "quality", mins, "hs", hard=True),
        steps=[StepSpec(kind="warmup", duration=f"{wu}mi", target=EASY),
               *body,
               StepSpec(kind="cooldown", duration=f"{cd}mi", target=EASY)],
    )


def _long(miles: float) -> WorkoutSpec:
    miles = round(miles, 1)
    mins = int(miles * 8.5)
    return WorkoutSpec(
        name=f"Long {miles}mi",
        estimated_minutes=mins,
        description=annotate(None, "long", mins, "hs", hard=True),
        steps=[StepSpec(kind="run", duration=f"{miles}mi", target=EASY)],
    )


def _time_trial(event: str, target: str) -> WorkoutSpec:
    dist = {"800": "800m", "1600": "1600m", "3200": "3200m", "5k": "5km"}[event]
    return WorkoutSpec(
        name=f"TIME TRIAL — {dist}",
        estimated_minutes=40,
        description=annotate("Race it. This is the checkpoint the next block is built on.",
                             "race", 40, "hs", hard=True),
        steps=[StepSpec(kind="warmup", duration="2mi", target=EASY),
               StepSpec(kind="run", duration=dist, target=target),
               StepSpec(kind="cooldown", duration="1.5mi", target=EASY)],
    )


def _race(event: str, target: str, label: str = "MEET") -> WorkoutSpec:
    dist = {"800": "800m", "1600": "1600m", "3200": "3200m", "5k": "5km"}[event]
    return WorkoutSpec(
        name=f"{label} — {dist}",
        estimated_minutes=45,
        description=annotate("Nothing new today. Warm up the way you practised.",
                             "race", 45, "hs", hard=True),
        steps=[StepSpec(kind="warmup", duration="2mi", target=EASY),
               StepSpec(kind="run", duration=dist, target=target),
               StepSpec(kind="cooldown", duration="1.5mi", target=EASY)],
    )


# --------------------------------------------------------------------------- #
# event-specific quality menus
# --------------------------------------------------------------------------- #
def _quality(event: str, phase: str, week_in_phase: int, t: dict) -> WorkoutSpec:
    """Quality session 1 — the event-specific one."""
    n = week_in_phase
    if phase == "base":
        return _track(f"{6 + n % 3}x400 @ 3200 pace — rhythm", 6 + n % 3, "400m",
                      "90s", t["P3200"])
    if phase == "strength":
        if event == "800":
            return _track(f"{5 + n % 2}x600 @ 1600 pace", 5 + n % 2, "600m", "2:30",
                          t["P1600"])
        if event == "1600":
            return _track(f"{5 + n % 2}x800 @ 3200 pace", 5 + n % 2, "800m", "2:00",
                          t["P3200"])
        return _track(f"{4 + n % 3}x1000m @ 5K pace", 4 + n % 3, "1000m", "2:30",
                      t["P5K"])
    if phase == "specific":
        if event == "800":
            return _track("3x(3x300 @ 800 pace)", 3, "300m", "2:00", t["RACE"],
                          sets=3, set_rest="5:00")
        if event == "1600":
            return _track("5x400 @ 1600 pace + 2x200 @ rep", 5, "400m", "2:00",
                          t["RACE"])
        if event == "3200":
            return _track("6x800 @ 3200 pace", 6, "800m", "2:30", t["RACE"])
        return _track("5x1000m @ 5K pace", 5, "1000m", "2:00", t["RACE"])
    if phase == "sharpen":
        if event == "800":
            return _track("4x200 @ rep + 1x400 @ 800 pace", 4, "200m", "2:00", t["REP"])
        return _track(f"{4}x400 @ race pace — sharpener", 4, "400m", "2:30", t["RACE"])
    # peak / championship week
    return _track("3x300 @ race pace — legs on", 3, "300m", "3:00", t["RACE"],
                  wu=1.5, cd=1.0)


def _second_quality(event: str, phase: str, week_in_phase: int, t: dict,
                    miles: float) -> WorkoutSpec:
    """Quality session 2 — aerobic support, the same for every event.

    This is the session high schoolers skip and then wonder why the last 200
    falls apart. Even 800 runners live off it.
    """
    if phase in ("base", "strength"):
        minutes = 12 + 2 * min(4, week_in_phase)
        return WorkoutSpec(
            name=f"{minutes}min tempo @ threshold",
            estimated_minutes=minutes + 25,
            description=annotate("Comfortably hard — you could speak a sentence, "
                                 "not hold a conversation.", "quality",
                                 minutes + 25, "hs", hard=True),
            steps=[StepSpec(kind="warmup", duration="1.5mi", target=EASY),
                   StepSpec(kind="run", duration=f"{minutes}:00", target=t["THRESH"]),
                   StepSpec(kind="cooldown", duration="1.5mi", target=EASY)],
        )
    if phase == "specific":
        return _track("4x1000m cruise @ threshold", 4, "1000m", "60s", t["THRESH"])
    return _easy(max(3.0, miles * 0.15), strides=6)


# --------------------------------------------------------------------------- #
# the generator
# --------------------------------------------------------------------------- #
PHASES = ["base", "strength", "specific", "sharpen", "peak"]


def phase_map(weeks: int) -> list[str]:
    """Split a block into the five track phases. Short blocks give up base first."""
    if weeks <= 4:
        return ["specific"] * (weeks - 1) + ["peak"]
    if weeks <= 8:
        specific = weeks - 4
        return ["strength"] * 2 + ["specific"] * specific + ["sharpen"] * 1 + ["peak"]
    base = max(2, int(weeks * 0.25))
    strength = max(2, int(weeks * 0.22))
    sharpen = 2
    peak = 1
    specific = max(2, weeks - base - strength - sharpen - peak)
    out = (["base"] * base + ["strength"] * strength + ["specific"] * specific
           + ["sharpen"] * sharpen + ["peak"] * peak)
    return out[:weeks] if len(out) >= weeks else out + ["peak"] * (weeks - len(out))


def build_track_plan(
    event: str = "1600",
    goal_time: Optional[str] = None,
    weeks: int = 16,
    start_date: Optional[str] = None,
    current_pr: Optional[str] = None,
    current_weekly_miles: float = 0.0,
    training_age: str = "developing",
    aerobic_reference: Optional[tuple[str, str]] = None,
    meet_days: Optional[list[str]] = None,
    today: Optional[date] = None,
) -> tuple[TrainingPlan, dict]:
    """Build a high-school track block toward a goal time.

    `event` is one of 800 / 1600 / 3200 / 5k. `goal_time` defaults to that
    event's barrier preset. `meet_days` is a list of ISO dates — those days get
    a race and the surrounding week loses its second quality session.

    Returns `(plan, report)`.
    """
    if event not in EVENT_MI:
        raise ValueError(f"event must be one of {sorted(EVENT_MI)}, got {event!r}")
    if training_age not in IMPROVEMENT_RATE:
        raise ValueError(f"training_age must be one of {sorted(IMPROVEMENT_RATE)}")
    goal_time = goal_time or PRESET_GOALS[event]
    today = today or date.today()

    monday = (date.fromisoformat(start_date) if start_date
              else today + timedelta(days=(7 - today.weekday()) % 7 or 7))
    if monday.weekday() != 0:
        raise ValueError(f"start_date must be a Monday, got {monday} ({monday:%A})")

    t = event_paces(event, goal_time, aerobic_reference)
    assessment = assess_track_gap(event, goal_time, weeks, current_pr,
                                  current_weekly_miles, training_age)
    ceiling = MILEAGE_CEILING[event][training_age]
    start_miles = max(10.0, current_weekly_miles or ceiling * 0.55)
    phases = phase_map(weeks)
    meets = {date.fromisoformat(d) for d in (meet_days or [])}

    items: list[tuple[str, object]] = []
    week_rows: list[dict] = []
    phase_counter: dict[str, int] = {}
    miles = start_miles

    for w in range(weeks):
        phase = phases[w]
        phase_counter[phase] = phase_counter.get(phase, 0) + 1
        week_in_phase = phase_counter[phase]
        monday_w = monday + timedelta(days=7 * w)
        week_meets = {d for d in meets if 0 <= (d - monday_w).days < 7}
        down = (week_in_phase % 4 == 0) and phase not in ("sharpen", "peak")
        taper = phase in ("sharpen", "peak")

        if phase in ("sharpen", "peak"):
            week_miles = round(miles * (0.80 if phase == "sharpen" else 0.62), 1)
        elif down:
            week_miles = round(miles * 0.78, 1)
        else:
            week_miles = round(min(miles, ceiling), 1)
            miles = min(miles * MAX_WEEKLY_RAMP, ceiling)

        long_miles = min(round(week_miles * 0.22, 1), 12.0)
        sessions: dict[int, WorkoutSpec] = {
            MON: _easy(week_miles * 0.14, strides=6),
            TUE: _quality(event, phase, week_in_phase, t),
            WED: _easy(week_miles * 0.14),
            THU: _second_quality(event, phase, week_in_phase, t, week_miles),
            FRI: _easy(max(2.5, week_miles * 0.10),
                       name="Pre-meet shakeout" if week_meets else None),
            SAT: _long(long_miles),
            # SUN is always rest. Never scheduled.
        }

        for d in week_meets:
            dow = d.weekday()
            if dow == SUN:
                continue
            sessions[dow] = _race(event, t["RACE"])
            if dow in (SAT, FRI):
                sessions[THU] = _easy(max(2.5, week_miles * 0.10),
                                      name="Pre-meet shakeout")

        # Mid-block checkpoint: a time trial at the end of the specific phase if
        # no meets are on the calendar at all.
        if not meets and phase == "specific" and week_in_phase == 3:
            sessions[SAT] = _time_trial(event, t["RACE"])

        for dow, spec in sorted(sessions.items()):
            items.append(((monday_w + timedelta(days=dow)).isoformat(), spec))

        lifts = strength_for_week(phase if phase in ("base", "build") else "build",
                                  down_week=down or taper,
                                  race_week=(phase == "peak"), level="hs")
        for dow, lift in zip((TUE, THU), lifts):
            items.append(((monday_w + timedelta(days=dow)).isoformat(), lift))

        week_rows.append({
            "week": w + 1,
            "starting": monday_w.isoformat(),
            "phase": phase,
            "target_miles": week_miles,
            "down_week": down,
            "meets": sorted(d.isoformat() for d in week_meets),
            "strength_sessions": [s.name for s in lifts],
            "rest_day": "Sunday — full rest, every week",
            "brief": weekly_brief(phase, week_miles, "hs", down_week=down,
                                  race_week=(phase == "peak")),
        })

    plan = TrainingPlan(
        name=f"HS {event} — sub-{goal_time} block",
        items=sorted(items, key=lambda it: it[0]),
    )
    report = {
        "event": event,
        "goal": goal_time,
        "goal_splits": splits(event, goal_time),
        "weeks": weeks,
        "starts": monday.isoformat(),
        "training_age": training_age,
        "weekly_mileage_ceiling": ceiling,
        "assessment": assessment,
        "paces": {k: v for k, v in t.items() if not k.startswith("_")},
        "aerobic_paces_from_reference": t["_aerobic_from_reference"],
        "pace_caveat": (
            "Race-pace reps come from the goal. Aerobic paces come from a supplied "
            "current result when there is one; otherwise they are extrapolated from "
            "the goal, which flatters endurance for 800/1600 runners — supply "
            "aerobic_reference=('3200','10:40') or similar for a sharper plan."
        ),
        "safety": {
            "rest_day": "Sunday is a full rest day in every week of this block.",
            "doubles": "None scheduled. Two-a-days are not part of this plan.",
            "hard_days_per_week": "Two, plus a meet at most.",
            "mileage_ceiling": f"{ceiling} mi/wk for a {training_age} {event} runner.",
            "growth_spurts": "Hold mileage flat through one; don't climb.",
            "coach": "Take this to your school coach before running it.",
            "pain": "Bone pain that hurts on a single-leg hop means stop and get "
                    "it looked at, not push through.",
        },
        "weeks_detail": week_rows,
        "sessions": len(items),
    }
    return plan, report
