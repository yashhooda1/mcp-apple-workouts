"""Running workout specs — the same friendly schema as mcp-garmin, so plans and
prompts written for one work on the other. Compilation to Apple's WorkoutKit
shape lives in `apple.py`.

A "spec" is what an LLM (or YAML/JSON) provides:

    WorkoutSpec(
        name="4x1mi threshold",
        steps=[
            StepSpec(kind="warmup",  duration="15:00", target="hr:2"),
            RepeatSpec(repeat=4, steps=[
                StepSpec(kind="run",      duration="1mi",  target="pace:6:35-6:55"),
                StepSpec(kind="recovery", duration="2:30", target="hr:2"),
            ]),
            StepSpec(kind="cooldown", duration="10:00", target="hr:2"),
        ],
    )

Durations:  "15:00"/"90s" = time, "1mi"/"1.5mi"/"400m"/"1km" = distance,
            "open" = no goal (ends when you press the lap button).
Targets:    None/"none", "hr:2"/"z2" = HR zone, "pace:6:35-6:55" = pace window.
"""
from __future__ import annotations

from typing import List, Optional, Union

from pydantic import BaseModel, Field

M_PER_MILE = 1609.34
M_PER_KM = 1000.0

STEP_KINDS = ("warmup", "run", "interval", "recovery", "cooldown", "rest")


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def pace_to_mps(pace: str) -> float:
    """'6:52' (min/mi) -> metres/sec."""
    minutes, seconds = pace.strip().split(":")
    return M_PER_MILE / (int(minutes) * 60 + int(seconds))


def parse_duration(text: str):
    """Return ('time', seconds), ('distance', metres) or ('open', 0)."""
    t = text.strip().lower()
    if t in ("open", "lap", "lap button"):
        return "open", 0.0
    if t.endswith("mi"):
        return "distance", float(t[:-2]) * M_PER_MILE
    if t.endswith("km"):
        return "distance", float(t[:-2]) * M_PER_KM
    if t.endswith("m"):
        return "distance", float(t[:-1])
    if t.endswith("s"):
        return "time", float(t[:-1])
    if ":" in t:
        parts = [int(p) for p in t.split(":")]
        total = 0
        for p in parts:
            total = total * 60 + p
        return "time", float(total)
    return "time", float(t)


def parse_target(text: Optional[str]) -> Optional[dict]:
    """Return a platform-neutral alert dict, or None for no target.

    {"type": "hr_zone", "zone": 2}
    {"type": "speed", "min_mps": 3.89, "max_mps": 4.07}   # slow pace -> min speed
    """
    if not text or text.strip().lower() == "none":
        return None
    t = text.strip().lower()
    if t.startswith("hr:") or (t.startswith("z") and t[1:].isdigit()):
        zone = int(t.split(":")[1]) if ":" in t else int(t[1:])
        if not 1 <= zone <= 5:
            raise ValueError(f"HR zone must be 1-5, got {zone}")
        return {"type": "hr_zone", "zone": zone}
    if t.startswith("pace:"):
        lo_s, hi_s = t.split(":", 1)[1].split("-")
        speeds = sorted([pace_to_mps(lo_s), pace_to_mps(hi_s)])
        return {"type": "speed", "min_mps": round(speeds[0], 3),
                "max_mps": round(speeds[1], 3)}
    raise ValueError(f"Unrecognised target: {text!r}")


# --------------------------------------------------------------------------- #
# spec models (the tool-facing schema)
# --------------------------------------------------------------------------- #
class StepSpec(BaseModel):
    kind: str = Field(description="warmup | run | recovery | cooldown | rest")
    duration: str = Field(
        description="'15:00'/'90s' for time, '1mi'/'400m'/'1km' for distance, 'open'"
    )
    target: Optional[str] = Field(
        default=None, description="'hr:2', 'pace:6:35-6:55', or null"
    )
    label: Optional[str] = Field(
        default=None, description="optional name shown on the Watch for this step"
    )


class RepeatSpec(BaseModel):
    repeat: int = Field(description="number of iterations")
    steps: List[StepSpec]


class WorkoutSpec(BaseModel):
    name: str
    steps: List[Union[StepSpec, RepeatSpec]]
    description: Optional[str] = None
    estimated_minutes: Optional[int] = None
    location: str = Field(default="outdoor", description="outdoor | indoor (treadmill)")


def validate_workout(spec: WorkoutSpec) -> None:
    """Fail fast on anything the Watch can't run."""
    if not spec.steps:
        raise ValueError(f"{spec.name}: a workout needs at least one step")
    if spec.location not in ("outdoor", "indoor"):
        raise ValueError(f"{spec.name}: location must be 'outdoor' or 'indoor'")
    for item in spec.steps:
        children = item.steps if isinstance(item, RepeatSpec) else [item]
        if isinstance(item, RepeatSpec) and item.repeat < 1:
            raise ValueError(f"{spec.name}: repeat must be >= 1")
        for s in children:
            if s.kind.lower() not in STEP_KINDS:
                raise ValueError(f"{spec.name}: unknown step kind {s.kind!r}")
            parse_duration(s.duration)
            parse_target(s.target)


# --------------------------------------------------------------------------- #
# ergonomic session builders (used by plans/)
# --------------------------------------------------------------------------- #
def easy(name: str, miles: float, strides: int = 0, hr_zone: int = 2) -> WorkoutSpec:
    steps: list = [StepSpec(kind="run", duration=f"{miles}mi", target=f"hr:{hr_zone}")]
    if strides:
        steps.append(
            RepeatSpec(repeat=strides, steps=[
                StepSpec(kind="run", duration="20s", target=None),
                StepSpec(kind="recovery", duration="60s", target=f"hr:{hr_zone}"),
            ])
        )
    return WorkoutSpec(name=name, steps=steps, estimated_minutes=int(miles * 8.5))


def long_run(name: str, miles: float, mp_pace: Optional[str] = None,
             mp_miles: float = 0) -> WorkoutSpec:
    easy_miles = round(miles - mp_miles, 2)
    steps: list = [StepSpec(kind="run", duration=f"{easy_miles}mi", target="hr:2")]
    if mp_miles:
        steps.append(StepSpec(kind="run", duration=f"{mp_miles}mi", target=f"pace:{mp_pace}"))
    return WorkoutSpec(name=name, steps=steps, estimated_minutes=int(miles * 8.5))


def intervals(name: str, n: int, rep: str, rep_pace: str, rec: str,
              wu: str = "15:00", cd: str = "10:00", est_min: int = 60) -> WorkoutSpec:
    return WorkoutSpec(
        name=name,
        estimated_minutes=est_min,
        steps=[
            StepSpec(kind="warmup", duration=wu, target="hr:2"),
            RepeatSpec(repeat=n, steps=[
                StepSpec(kind="run", duration=rep, target=f"pace:{rep_pace}"),
                StepSpec(kind="recovery", duration=rec, target="hr:2"),
            ]),
            StepSpec(kind="cooldown", duration=cd, target="hr:2"),
        ],
    )
