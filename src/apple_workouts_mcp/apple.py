"""Compile workout specs into the JSON shape HoodaRun turns into WorkoutKit.

Apple has no cloud API that writes to someone's Watch. Structured workouts
reach it through WorkoutKit on the iPhone: a `CustomWorkout` is built on-device
and handed to `WorkoutScheduler`, which syncs it to the Watch's Workout app.
So this server's job is to emit something that maps 1:1 onto WorkoutKit, and
the HoodaRun app (see `ios/`) does the last hop.

The emitted shape mirrors WorkoutKit's types directly:

    {
      "activity": "running",                 # HKWorkoutActivityType
      "location": "outdoor",                 # HKWorkoutSessionLocationType
      "display_name": "4x1mi threshold",
      "warmup":   <Step> | null,             # CustomWorkout.warmup
      "blocks":   [                          # [IntervalBlock]
        {"iterations": 4, "steps": [         #   IntervalBlock.iterations / steps
          {"purpose": "work",     "step": <Step>},   # IntervalStep(.work, ...)
          {"purpose": "recovery", "step": <Step>}
        ]}
      ],
      "cooldown": <Step> | null
    }

    Step  = {"goal": Goal, "alert": Alert | null, "display_name": str | null}
    Goal  = {"type": "time", "seconds": 900}
          | {"type": "distance", "meters": 1609.34}
          | {"type": "open"}
    Alert = {"type": "hr_zone", "zone": 2}                       # HeartRateZoneAlert
          | {"type": "speed", "min_mps": 3.89, "max_mps": 4.07}  # SpeedRangeAlert

WorkoutKit only allows a single warmup and a single cooldown step around the
blocks, so a leading `warmup` and a trailing `cooldown` lift out of the step
list; everything between becomes blocks, with consecutive plain steps sharing a
one-iteration block so the step order on the wrist matches the spec exactly.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .strength import StrengthSpec, estimate_seconds, parse_seconds, validate_strength
from .workouts import (
    RepeatSpec,
    StepSpec,
    WorkoutSpec,
    parse_duration,
    parse_target,
    validate_workout,
)

SCHEMA = "hooda.workouts.custom/v1"

_PURPOSE = {
    "warmup": "work",
    "run": "work",
    "interval": "work",
    "cooldown": "work",
    "recovery": "recovery",
    "rest": "recovery",
}


def _goal(duration: str) -> dict:
    kind, value = parse_duration(duration)
    if kind == "time":
        return {"type": "time", "seconds": round(value, 1)}
    if kind == "distance":
        return {"type": "distance", "meters": round(value, 2)}
    return {"type": "open"}


def _step(spec: StepSpec) -> dict:
    return {
        "goal": _goal(spec.duration),
        "alert": parse_target(spec.target),
        "display_name": spec.label,
    }


def _interval(spec: StepSpec) -> dict:
    return {"purpose": _PURPOSE[spec.kind.lower()], "step": _step(spec)}


def compile_workout(spec: WorkoutSpec) -> dict:
    """WorkoutSpec -> WorkoutKit-shaped CustomWorkout JSON."""
    validate_workout(spec)
    items = list(spec.steps)

    warmup = None
    if isinstance(items[0], StepSpec) and items[0].kind.lower() == "warmup" and len(items) > 1:
        warmup = _step(items.pop(0))
    cooldown = None
    # keep at least one item for the blocks: WorkoutKit needs a main set
    if isinstance(items[-1], StepSpec) and items[-1].kind.lower() == "cooldown" \
            and len(items) > 1:
        cooldown = _step(items.pop())

    blocks: list[dict] = []
    run_of_singles: list[dict] = []
    for item in items:
        if isinstance(item, RepeatSpec):
            if run_of_singles:
                blocks.append({"iterations": 1, "steps": run_of_singles})
                run_of_singles = []
            blocks.append({
                "iterations": item.repeat,
                "steps": [_interval(s) for s in item.steps],
            })
        else:
            run_of_singles.append(_interval(item))
    if run_of_singles:
        blocks.append({"iterations": 1, "steps": run_of_singles})

    return {
        "schema": SCHEMA,
        "activity": "running",
        "location": spec.location,
        "display_name": spec.name,
        "warmup": warmup,
        "blocks": blocks,
        "cooldown": cooldown,
        "estimated_seconds": (spec.estimated_minutes or 45) * 60,
    }


def compile_strength(spec: StrengthSpec) -> dict:
    """StrengthSpec -> WorkoutKit-shaped strength CustomWorkout JSON.

    One block per exercise, `iterations = sets`. Rep sets are open steps (end
    the set with the Action button); holds are timed. Rest is a timed recovery
    step. HoodaRun falls back to functional strength if the Watch doesn't
    support traditional strength as a custom workout.
    """
    validate_strength(spec)
    blocks = []
    for ex in spec.exercises:
        name = (ex.exercise or ex.category).replace("_", " ").title()
        if ex.hold:
            work = {"goal": {"type": "time", "seconds": parse_seconds(ex.hold)},
                    "alert": None, "display_name": f"{name} · {ex.hold} hold"}
        else:
            load = f" @ {ex.weight_kg:g} kg" if ex.weight_kg else ""
            work = {"goal": {"type": "open"}, "alert": None,
                    "display_name": f"{name} · {ex.reps} reps{load}"}
        steps = [{"purpose": "work", "step": work}]
        if ex.rest_s:
            steps.append({"purpose": "recovery",
                          "step": {"goal": {"type": "time", "seconds": float(ex.rest_s)},
                                   "alert": None, "display_name": "Rest"}})
        blocks.append({"iterations": ex.sets, "steps": steps})
    return {
        "schema": SCHEMA,
        "activity": "traditional_strength_training",
        "fallback_activity": "functional_strength_training",
        "location": "indoor",
        "display_name": spec.name,
        "warmup": None,
        "blocks": blocks,
        "cooldown": None,
        "estimated_seconds": estimate_seconds(spec),
    }


def compile_any(spec) -> dict:
    return compile_strength(spec) if isinstance(spec, StrengthSpec) else compile_workout(spec)


def sport_of(spec) -> str:
    return "strength" if isinstance(spec, StrengthSpec) else "running"


def parse_spec(raw) -> WorkoutSpec | StrengthSpec:
    """Accept a dict (from an MCP call) or an existing spec model."""
    if isinstance(raw, (WorkoutSpec, StrengthSpec)):
        return raw
    if isinstance(raw, dict) and "exercises" in raw:
        return StrengthSpec.model_validate(raw)
    return WorkoutSpec.model_validate(raw)


def revision(workout: dict, description: Optional[str], time: Optional[str]) -> str:
    """Content hash: HoodaRun reschedules an item only when this changes."""
    blob = json.dumps({"w": workout, "d": description, "t": time}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def step_count(workout: dict) -> int:
    n = sum(len(b["steps"]) * b["iterations"] for b in workout["blocks"])
    return n + (workout["warmup"] is not None) + (workout["cooldown"] is not None)
