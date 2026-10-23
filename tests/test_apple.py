"""The compiler's output must map 1:1 onto WorkoutKit's types."""
import pytest

from apple_workouts_mcp.apple import compile_strength, compile_workout, parse_spec, step_count
from apple_workouts_mcp.strength import Hold, Lift, StrengthSpec, strength_a
from apple_workouts_mcp.workouts import (
    RepeatSpec,
    StepSpec,
    WorkoutSpec,
    easy,
    intervals,
    pace_to_mps,
)


def _threshold():
    return intervals("4x1mi threshold", 4, "1mi", "6:35-6:55", "2:30")


def test_warmup_and_cooldown_lift_out_of_blocks():
    w = compile_workout(_threshold())
    assert w["activity"] == "running" and w["location"] == "outdoor"
    assert w["warmup"]["goal"] == {"type": "time", "seconds": 900.0}
    assert w["warmup"]["alert"] == {"type": "hr_zone", "zone": 2}
    assert w["cooldown"]["goal"] == {"type": "time", "seconds": 600.0}
    assert len(w["blocks"]) == 1
    block = w["blocks"][0]
    assert block["iterations"] == 4
    assert [s["purpose"] for s in block["steps"]] == ["work", "recovery"]
    assert block["steps"][0]["step"]["goal"] == {"type": "distance", "meters": 1609.34}


def test_pace_window_becomes_speed_range_slow_first():
    alert = compile_workout(_threshold())["blocks"][0]["steps"][0]["step"]["alert"]
    assert alert["type"] == "speed"
    assert alert["min_mps"] == pytest.approx(pace_to_mps("6:55"), abs=1e-3)
    assert alert["max_mps"] == pytest.approx(pace_to_mps("6:35"), abs=1e-3)
    assert alert["min_mps"] < alert["max_mps"]


def test_easy_run_is_a_single_block_with_no_warmup():
    w = compile_workout(easy("Easy 6mi", 6, strides=6))
    assert w["warmup"] is None and w["cooldown"] is None
    assert [b["iterations"] for b in w["blocks"]] == [1, 6]


def test_step_order_is_preserved_between_repeats():
    spec = WorkoutSpec(name="Mixed", steps=[
        StepSpec(kind="warmup", duration="2mi", target="hr:2"),
        StepSpec(kind="run", duration="3mi", target="pace:6:20-6:30"),
        RepeatSpec(repeat=2, steps=[StepSpec(kind="run", duration="400m"),
                                    StepSpec(kind="rest", duration="open")]),
        StepSpec(kind="run", duration="1mi", target="hr:3", label="float"),
        StepSpec(kind="cooldown", duration="1mi", target="hr:2"),
    ])
    w = compile_workout(spec)
    assert [b["iterations"] for b in w["blocks"]] == [1, 2, 1]
    assert w["blocks"][1]["steps"][1]["step"]["goal"] == {"type": "open"}
    assert w["blocks"][2]["steps"][0]["step"]["display_name"] == "float"
    assert step_count(w) == 1 + 1 + 4 + 1 + 1


def test_lone_cooldown_still_leaves_a_main_set():
    w = compile_workout(WorkoutSpec(name="Shakeout", steps=[
        StepSpec(kind="cooldown", duration="20:00", target="hr:1")]))
    assert w["cooldown"] is None and len(w["blocks"]) == 1


def test_indoor_location_and_bad_inputs():
    w = compile_workout(WorkoutSpec(name="Treadmill", location="indoor",
                                    steps=[StepSpec(kind="run", duration="5mi")]))
    assert w["location"] == "indoor"
    with pytest.raises(ValueError):
        compile_workout(WorkoutSpec(name="x", steps=[StepSpec(kind="sprint", duration="1mi")]))
    with pytest.raises(ValueError):
        compile_workout(WorkoutSpec(name="x", steps=[StepSpec(kind="run", duration="1mi",
                                                             target="hr:7")]))
    with pytest.raises(ValueError):
        compile_workout(WorkoutSpec(name="x", steps=[]))


def test_strength_one_block_per_exercise():
    spec = StrengthSpec(name="Core", exercises=[
        Lift("DEADLIFT", "ROMANIAN_DEADLIFT", sets=4, reps=8, rest_s=90, weight_kg=40),
        Hold("PLANK", "SIDE_PLANK", sets=3, hold="30s", rest_s=30),
    ])
    w = compile_strength(spec)
    assert w["activity"] == "traditional_strength_training"
    assert w["fallback_activity"] == "functional_strength_training"
    rdl, plank = w["blocks"]
    assert rdl["iterations"] == 4
    assert rdl["steps"][0]["step"]["goal"] == {"type": "open"}
    assert rdl["steps"][0]["step"]["display_name"] == "Romanian Deadlift · 8 reps @ 40 kg"
    assert rdl["steps"][1]["step"]["goal"] == {"type": "time", "seconds": 90.0}
    assert plank["steps"][0]["step"]["goal"] == {"type": "time", "seconds": 30.0}


def test_library_sessions_compile():
    w = compile_strength(strength_a("build"))
    assert len(w["blocks"]) == 6 and w["estimated_seconds"] > 0


def test_parse_spec_dispatches_on_exercises():
    assert isinstance(parse_spec({"name": "s", "exercises": [
        {"category": "PLANK", "hold": "45s"}]}), StrengthSpec)
    assert isinstance(parse_spec({"name": "r", "steps": [
        {"kind": "run", "duration": "3mi"}]}), WorkoutSpec)
