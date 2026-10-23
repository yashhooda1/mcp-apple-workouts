"""Strength + core sessions: spec models and the runner-specific library every
plan in this repo pulls from (shared with mcp-garmin).

Rule of the repo: **every training plan ships two strength sessions a week.**
`apple.compile_strength` turns a StrengthSpec into a WorkoutKit strength
workout: one named step per set (e.g. "RDL — set 2/4 · 8 reps") with a timed
rest step between sets, so the Watch walks through the session exercise by
exercise instead of showing a note that says "do some core".

    StrengthSpec(
        name="Strength A — posterior chain",
        exercises=[
            Lift("DEADLIFT", "ROMANIAN_DEADLIFT", sets=3, reps=8, rest_s=90),
            Hold("PLANK", "PLANK", sets=3, hold="45s", rest_s=45),
        ],
    )

Two end conditions per exercise:
  * `reps`  — rep-counted set: an open step you end with the Action button
  * `hold`  — isometric/timed set, "45s"/"1:00": a timed step

Category / exercise names keep the Garmin-style ids so plans port unchanged;
`label()` turns them into the human name the Watch displays.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .fueling import annotate


def parse_seconds(text: str) -> float:
    """'45s' / '1:00' / '90' -> seconds."""
    t = text.strip().lower()
    if t.endswith("s"):
        return float(t[:-1])
    if ":" in t:
        mm, ss = t.split(":")
        return float(int(mm) * 60 + int(ss))
    return float(t)


# --------------------------------------------------------------------------- #
# spec models (the tool-facing schema)
# --------------------------------------------------------------------------- #
class ExerciseSpec(BaseModel):
    category: str = Field(description="Exercise category, e.g. 'PLANK', 'DEADLIFT'")
    exercise: str = Field(default="", description="Variant, e.g. 'ROMANIAN_DEADLIFT'")
    sets: int = Field(default=3, description="number of sets")
    reps: Optional[int] = Field(default=None, description="reps per set (rep-counted work)")
    hold: Optional[str] = Field(default=None, description="'45s'/'1:00' for isometric holds")
    rest_s: int = Field(default=60, description="rest between sets, seconds")
    weight_kg: Optional[float] = Field(default=None, description="target load, kg")
    note: Optional[str] = Field(default=None, description="coaching cue, shown in the description")

    def label(self) -> str:
        name = (self.exercise or self.category).replace("_", " ").title()
        dose = f"{self.reps}" if self.reps is not None else (self.hold or "")
        return f"{self.sets}x{dose} {name}"


class StrengthSpec(BaseModel):
    name: str
    exercises: List[ExerciseSpec]
    description: Optional[str] = None
    estimated_minutes: Optional[int] = None


def Lift(category: str, exercise: str = "", *, sets: int = 3, reps: int = 8,
         rest_s: int = 90, weight_kg: float | None = None,
         note: str | None = None) -> ExerciseSpec:
    """Rep-counted set."""
    return ExerciseSpec(category=category, exercise=exercise, sets=sets, reps=reps,
                        rest_s=rest_s, weight_kg=weight_kg, note=note)


def Hold(category: str, exercise: str = "", *, sets: int = 3, hold: str = "45s",
         rest_s: int = 45, note: str | None = None) -> ExerciseSpec:
    """Isometric / timed set."""
    return ExerciseSpec(category=category, exercise=exercise, sets=sets, hold=hold,
                        rest_s=rest_s, note=note)




def validate_strength(spec: StrengthSpec) -> None:
    if not spec.exercises:
        raise ValueError(f"{spec.name}: a strength session needs exercises")
    for ex in spec.exercises:
        if ex.reps is None and ex.hold is None:
            raise ValueError(f"{ex.category}: set either reps or hold")
        if ex.reps is not None and ex.hold is not None:
            raise ValueError(f"{ex.category}: reps and hold are mutually exclusive")
        if ex.sets < 1:
            raise ValueError(f"{ex.category}: sets must be >= 1")


def cue_summary(spec: StrengthSpec) -> str:
    """The coaching cues, joined — shown with the workout in HoodaRun."""
    return " | ".join(f"{ex.label()} — {ex.note}" for ex in spec.exercises if ex.note)


def estimate_seconds(spec: StrengthSpec) -> int:
    if spec.estimated_minutes:
        return spec.estimated_minutes * 60
    total = 0.0
    for ex in spec.exercises:
        work = parse_seconds(ex.hold) if ex.hold else (ex.reps or 10) * 3.0
        total += ex.sets * (work + ex.rest_s)
    return int(total)


# --------------------------------------------------------------------------- #
# the runner strength library
# --------------------------------------------------------------------------- #
# Design intent, so future edits don't drift:
#   * Two sessions a week, both ~25-35 min, both landing on quality-run days so
#     easy days stay genuinely easy and the hard/easy pattern survives.
#   * Day A is heavy, posterior-chain — the durability work that protects
#     hamstrings and calves under volume.
#   * Day B is unilateral hip/pelvis control + anti-rotation core — what keeps
#     form intact in the last 10K of a marathon or the last 200 of an 800.
#   * Nothing here needs more than dumbbells and a band. High-school athletes
#     and garage-gym adults are first-class users.

CUE_RDL = "Hinge, don't squat. Bar close, back flat, feel the hamstrings load."
CUE_SPLIT = "Front shin vertical, torso tall. Control the descent for 2s."
CUE_CALF = "Full stretch at the bottom, 1s pause at the top — calves take the hit late in a race."
CUE_HIP = "Ribs down, squeeze the glute. Do not arch the low back to finish the rep."
CUE_DEADBUG = "Low back stays flat on the floor the whole set. Stop the set when it lifts."
CUE_PLANK = "Straight line ear-to-ankle. End the set when the hips sag, not when the clock says so."
CUE_BAND = "Band at the ankles, small controlled steps, knees tracking over the toes."


def strength_a(phase: str = "build", level: str = "adult") -> StrengthSpec:
    """Heavy day: posterior chain + anti-extension core."""
    heavy = phase in ("base", "build")
    reps = 8 if heavy else 6
    sets = 3 if level == "hs" else (4 if heavy else 3)
    return StrengthSpec(
        name=f"Strength A — posterior chain + core ({phase})",
        estimated_minutes=30,
        exercises=[
            Lift("DEADLIFT", "ROMANIAN_DEADLIFT", sets=sets, reps=reps, rest_s=90,
                 note=CUE_RDL),
            Lift("LUNGE", "DUMBBELL_BULGARIAN_SPLIT_SQUAT", sets=3, reps=8, rest_s=75,
                 note=CUE_SPLIT),
            Lift("CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE", sets=3, reps=12,
                 rest_s=45, note=CUE_CALF),
            Lift("ROW", "SINGLE_ARM_NEUTRAL_GRIP_DUMBBELL_ROW", sets=3, reps=10, rest_s=60,
                 note="Posture work. A collapsed upper back is a mile-25 problem."),
            Hold("PLANK", "PLANK", sets=3, hold="45s", rest_s=45, note=CUE_PLANK),
            Lift("HIP_STABILITY", "DEAD_BUG", sets=3, reps=10, rest_s=45, note=CUE_DEADBUG),
        ],
    )


def strength_b(phase: str = "build", level: str = "adult") -> StrengthSpec:
    """Control day: single-leg hip/pelvis stability + anti-rotation core."""
    return StrengthSpec(
        name=f"Strength B — hips + anti-rotation core ({phase})",
        estimated_minutes=28,
        exercises=[
            Lift("HIP_RAISE", "SINGLE_LEG_HIP_RAISE", sets=3, reps=12, rest_s=45,
                 note=CUE_HIP),
            Lift("HIP_STABILITY", "LATERAL_WALKS_WITH_BAND_AT_ANKLES", sets=3, reps=12,
                 rest_s=45, note=CUE_BAND),
            Lift("LUNGE", "WALKING_LUNGE", sets=3, reps=10, rest_s=60,
                 note="10 per leg, slow and level — no hip drop."),
            Lift("PUSH_UP", "PUSH_UP", sets=3, reps=12, rest_s=45,
                 note="Body in one line; this is a plank that moves."),
            Hold("PLANK", "SIDE_PLANK", sets=3, hold="30s", rest_s=30,
                 note="Each side. Glute-med work that stops the knee collapsing inward."),
            Lift("HIP_STABILITY", "QUADRUPED_WITH_LEG_LIFT", sets=3, reps=10, rest_s=30,
                 note="Bird dog. Slow, no rotation through the hips."),
        ],
    )


def mobility_reset(level: str = "adult") -> StrengthSpec:
    """Short down-week / race-week session. Keeps the 2x/week habit without load."""
    return StrengthSpec(
        name="Strength C — mobility reset (down week)",
        estimated_minutes=18,
        description="Down/taper week. Movement quality only — leave the load in the rack.",
        exercises=[
            Lift("HIP_RAISE", "SINGLE_LEG_HIP_RAISE", sets=2, reps=10, rest_s=30,
                 note=CUE_HIP),
            Lift("HIP_STABILITY", "LATERAL_WALKS_WITH_BAND_AT_ANKLES", sets=2, reps=10,
                 rest_s=30, note=CUE_BAND),
            Hold("PLANK", "PLANK", sets=2, hold="40s", rest_s=40, note=CUE_PLANK),
            Lift("HIP_STABILITY", "DEAD_BUG", sets=2, reps=10, rest_s=30, note=CUE_DEADBUG),
            Lift("CALF_RAISE", "STANDING_CALF_RAISE", sets=2, reps=15, rest_s=30,
                 note="Blood flow, not stimulus."),
        ],
    )


def strength_for_week(phase: str, down_week: bool = False, race_week: bool = False,
                      level: str = "adult") -> list[StrengthSpec]:
    """The 2x/week prescription for one week of any plan in this repo.

    Down and race weeks swap the heavy day for the mobility reset — the habit
    never breaks, the load does.
    """
    if race_week:
        out = [mobility_reset(level)]
    elif down_week:
        out = [mobility_reset(level), strength_b(phase, level)]
    else:
        out = [strength_a(phase, level), strength_b(phase, level)]
    for spec in out:
        # Lifts get the same fuelling/recovery treatment as runs — protein after
        # a session is the half of it that actually builds the tissue.
        spec.description = annotate(cue_summary(spec) or spec.description,
                                    "strength", spec.estimated_minutes or 25, level)
    return out
