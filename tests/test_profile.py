"""The runner profile: validation, storage, and laying a plan onto the
runner's own days and times."""
from collections import Counter
from datetime import date

import pytest
from pydantic import ValidationError

from apple_workouts_mcp import profile as rp
from apple_workouts_mcp import server
from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.plans import build_items
from apple_workouts_mcp.profile import RunnerProfile, arrange_week, week_mapping
from apple_workouts_mcp.road import build_road_plan
from apple_workouts_mcp.strength import StrengthSpec

DOW = rp.DAYS


def _road(**kw):
    kw.setdefault("current_weekly_miles", 30)
    return build_road_plan("10k", goal_time="45:00", weeks=10, start_date="2026-11-16", **kw)


def _shape(plan, weeks=range(0, 7)):
    """{weekday: kind} over the build weeks (kind: long / quality / easy)."""
    start, seen = date(2026, 11, 16), {}
    for iso, spec in plan.items:
        d = date.fromisoformat(iso)
        if isinstance(spec, StrengthSpec) or (d - start).days // 7 not in weeks:
            continue
        kind = "long" if spec.name.startswith("Long") else \
            "quality" if ("interval" in spec.name or "tempo" in spec.name
                          or "repeats" in spec.name) else "easy"
        seen.setdefault(DOW[d.weekday()], set()).add(kind)
    return {k: "/".join(sorted(v)) for k, v in seen.items()}


# ------------------------------------------------------------------ validation
def test_days_and_times_are_normalised():
    p = RunnerProfile(run_days=["Sunday", "tue", 3, "SAT", "tue"], long_run_day="Saturday",
                      quality_days=["thurs", "Tuesday"], workout_time="5:45",
                      strength_time="18:00")
    assert p.run_days == ["tue", "thu", "sat", "sun"] and p.long_run_day == "sat"
    assert p.quality_days == ["tue", "thu"] and p.workout_time == "05:45"


@pytest.mark.parametrize("bad,why", [
    ({"run_days": ["mon", "funday", "sat"]}, "unknown day"),
    ({"run_days": ["mon", "tue"]}, "at least 3"),
    ({"quality_days": ["mon", "wed", "fri"]}, "at most 2"),
    ({"quality_days": ["sat"], "long_run_day": "sat"}, "can't also be"),
    ({"run_days": ["mon", "wed", "fri"], "long_run_day": "sun"}, "must also be in run_days"),
    ({"workout_time": "6am"}, "bad time"),
    ({"weekly_miles": 400}, "between 0 and 150"),
    ({"recent_race": {"distance": "50k", "time": "5:00:00"}}, "race distance"),
])
def test_bad_profiles_are_rejected(bad, why):
    with pytest.raises(ValidationError, match=why):
        RunnerProfile(**bad)


def test_an_old_race_is_not_used_as_a_benchmark():
    today = date(2026, 11, 16)
    fresh = RunnerProfile(recent_race={"distance": "5K", "time": "21:40", "date": "2026-10-03"})
    stale = RunnerProfile(recent_race={"distance": "5k", "time": "21:40", "date": "2025-10-03"})
    undated = RunnerProfile(recent_race={"distance": "5k", "time": "21:40"})
    assert fresh.benchmark(today) == ("21:40", "5k") == undated.benchmark(today)
    assert stale.benchmark(today) is None


def test_profile_round_trips_and_merges(tmp_path):
    store = FeedStore(tmp_path)
    assert rp.load(store) == RunnerProfile() and "weekly_miles" in rp.load(store).missing()
    rp.update(store, {"weekly_miles": 32, "long_run_day": "sat"})
    rp.update(store, {"workout_time": "05:45", "weekly_miles": None})   # None = leave alone
    saved = rp.load(store)
    assert (saved.weekly_miles, saved.long_run_day, saved.workout_time) == (32, "sat", "05:45")
    assert rp.update(store, {}, clear=["long_run_day"]).long_run_day is None
    with pytest.raises(ValueError, match="unknown profile field"):
        rp.update(store, {}, clear=["password"])
    with pytest.raises(ValidationError):                  # a merge is validated as a whole
        rp.update(store, {"run_days": ["mon", "wed", "fri"], "long_run_day": "sun"})
    assert rp.load(store).workout_time == "05:45"         # and a rejected one saves nothing


# ------------------------------------------------------------ arranging a week
def test_default_template_is_tue_thu_hard_sun_long():
    plan, _ = _road()
    assert _shape(plan) == {"mon": "easy", "tue": "quality", "thu": "quality",
                            "sat": "easy", "sun": "long"}
    assert arrange_week(plan, RunnerProfile(weekly_miles=30))[1] is None   # no day prefs


def test_long_run_and_quality_days_move_to_the_runners_days():
    plan, _ = _road()
    prefs = RunnerProfile(run_days=["mon", "wed", "fri", "sat", "sun"],
                          quality_days=["wed", "fri"], long_run_day="sun")
    arranged, info = arrange_week(plan, prefs)
    assert _shape(arranged) == {"mon": "easy", "wed": "quality", "fri": "quality",
                                "sat": "easy", "sun": "long"}
    assert info["quality"] == ["wed", "fri"] and info["long_run"] == "sun"

    sat_long, info = arrange_week(plan, RunnerProfile(long_run_day="sat"))
    shape = _shape(sat_long)
    assert shape["sat"] == "long" and shape["tue"] == shape["thu"] == "quality"
    assert sorted(shape) == sorted(["mon", "tue", "thu", "sat", "sun"])   # same five days
    assert "note" not in info


def test_arranging_changes_days_not_content():
    plan, _ = _road()
    arranged, _ = arrange_week(plan, RunnerProfile(long_run_day="sat", quality_days=["mon", "wed"]))
    assert Counter(s.name for _, s in arranged.items) == Counter(s.name for _, s in plan.items)
    assert len(build_items(arranged)) == len(plan.items)            # still no clashes
    # every session stays in its own Mon-Sun week
    def week(iso):
        return date.fromisoformat(iso).isocalendar()[:2]
    before = sorted((week(d), s.name) for d, s in plan.items)
    assert before == sorted((week(d), s.name) for d, s in arranged.items)


def test_strength_travels_with_its_hard_day_and_race_week_is_untouched():
    plan, _ = _road()
    arranged, _ = arrange_week(plan, RunnerProfile(quality_days=["wed", "fri"]))
    lifts = {date.fromisoformat(d).weekday() for d, s in arranged.items
             if isinstance(s, StrengthSpec) and d < "2027-01-04"}
    assert {DOW[d] for d in lifts} == {"wed", "fri"}
    race_week = lambda p: [(d, s.name) for d, s in p.items if d >= "2027-01-18"]  # noqa: E731
    assert race_week(arranged) == race_week(plan) and race_week(plan)[-1][1].startswith("RACE")


def test_back_to_back_hard_days_are_allowed_but_flagged():
    plan, _ = _road()
    _, info = arrange_week(plan, RunnerProfile(quality_days=["fri"], long_run_day="sat"))
    assert "back to back" in info["note"] and "fri→sat" in info["note"]


def test_base_tier_and_fewer_named_days_still_produce_a_valid_week():
    plan, report = _road(current_weekly_miles=12)          # base tier: 4 days, one quality
    assert report["tier"] == "base"
    arranged, info = arrange_week(plan, RunnerProfile(
        run_days=["mon", "tue", "wed", "thu", "fri", "sat"], quality_days=["wed", "fri"],
        long_run_day="sat"))
    shape = _shape(arranged)
    assert shape["sat"] == "long" and list(shape.values()).count("quality") == 1
    assert len(shape) == 4 and "sun" not in shape and len(build_items(arranged)) == len(plan.items)
    m = week_mapping({"long": 6, "quality": [1, 3]}, [0, 1, 3, 5, 6],
                     RunnerProfile(long_run_day="tue"))
    assert m[6] == 1 and len(set(m.values())) == 5 and 1 not in (m[1], m[3])


# -------------------------------------------------------------- through the tools
@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", lambda: FeedStore(tmp_path))
    return server


def test_plan_tools_use_the_saved_profile(tools):
    with pytest.raises(ValueError, match="current weekly mileage"):
        tools.create_road_race_plan("10k", goal_time="45:00", weeks=10, start_date="2026-11-16")

    out = tools.update_runner_profile(
        weekly_miles=32, run_days=["mon", "tue", "thu", "sat", "sun"], long_run_day="Saturday",
        quality_days=["tue", "thu"], workout_time="05:45", strength_time="18:00",
        recent_race_distance="5k", recent_race_time="21:40",
        recent_race_date=date.today().isoformat())
    assert out["saved"] and out["not_set"] == []
    assert tools.get_runner_profile()["profile"]["recent_race"]["time"] == "21:40"

    res = tools.create_road_race_plan("10k", goal_time="45:00", weeks=10,
                                      start_date="2026-11-16", dry_run=False)
    assert res["report"]["from_profile"] == {"current_weekly_miles": 32, "days_per_week": 5,
                                             "benchmark": "21:40 5k"}
    assert res["report"]["week_layout"]["long_run"] == "sat"
    assert "provisional" not in res["report"]["warning"]        # the benchmark was used
    rows = FeedStore(tools.store().dir).load()["workouts"]
    assert {w["time"] for w in rows if w["sport"] == "strength"} == {"18:00"}
    assert {w["time"] for w in rows if w["sport"] == "running"} == {"05:45"}
    longs = {date.fromisoformat(w["date"]).weekday() for w in rows if w["name"].startswith("Long")}
    assert longs == {5}

    # an explicit argument still wins over the profile
    explicit = tools.create_road_race_plan("10k", goal_time="45:00", weeks=10,
                                           start_date="2026-11-16", current_weekly_miles=40,
                                           time="07:00")
    assert "current_weekly_miles" not in explicit["report"]["from_profile"]
    from apple_workouts_mcp.workouts import easy
    one = tools.create_workout(easy("Shakeout", 3), "2026-12-30")
    assert one["time"] == "05:45"


def test_update_profile_reports_problems_in_plain_words(tools):
    with pytest.raises(ValueError, match="can't also be a quality day"):
        tools.update_runner_profile(long_run_day="sat", quality_days=["sat"])
    with pytest.raises(ValueError, match="needs both"):
        tools.update_runner_profile(recent_race_time="21:40")
    tools.update_runner_profile(recent_race_distance="5k", recent_race_time="21:40",
                                recent_race_date="2026-10-03")
    newer = tools.update_runner_profile(recent_race_time="21:10")
    assert newer["profile"]["recent_race"] == {"distance": "5k", "time": "21:10"}


def test_marathon_tool_takes_mileage_and_days_from_the_profile(tools):
    tools.update_runner_profile(weekly_miles=35, long_run_day="sat",
                                run_days=["mon", "tue", "wed", "thu", "sat", "sun"])
    res = tools.create_marathon_plan("Test Marathon", "2027-04-18", "3:45:00")
    assert res["report"]["from_profile"] == {"current_weekly_miles": 35, "days_per_week": 6}
    assert res["report"]["week_layout"]["long_run"] == "sat"
