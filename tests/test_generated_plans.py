"""Offline tests: generated marathon and HS track blocks build, serialize, and
keep the promises the modules make — two strength sessions a week, fuelling on
every session, and conservative handling of high-school athletes."""
from collections import Counter
from datetime import date, timedelta

import pytest

from apple_workouts_mcp.fitness import summarize_fitness
from apple_workouts_mcp.fueling import weekly_brief
from apple_workouts_mcp.plans import preview_plan
from apple_workouts_mcp.strength import StrengthSpec
from plans import races
from plans.marathon import build_marathon_plan
from plans.track_hs import MILEAGE_CEILING, build_track_plan, event_paces, splits
from tests.test_fitness import _runs

TODAY = date(2026, 9, 15)
MONDAY = date(2026, 9, 21)


def _weeks_of(plan, monday=MONDAY):
    """Group scheduled items by week index."""
    out: dict[int, list] = {}
    for iso, spec in plan.items:
        idx = (date.fromisoformat(iso) - monday).days // 7
        out.setdefault(idx, []).append(spec)
    return out


# --------------------------------------------------------------------------- #
# marathon
# --------------------------------------------------------------------------- #
def _marathon(goal="3:30:00", mpw=30, weeks=18):
    snap = summarize_fitness(_runs(mpw, today=TODAY), weeks=8, today=TODAY)
    race = MONDAY + timedelta(weeks=weeks - 1, days=6)
    return build_marathon_plan(
        race_name="Test Marathon", race_date=race.isoformat(), goal_time=goal,
        snapshot=snap, start_date=MONDAY.isoformat(), today=TODAY,
    )


def test_marathon_plan_builds_and_serializes():
    plan, report = _marathon()
    rows = preview_plan(plan)                     # compiles every workout
    assert len(rows) == len(plan.items)
    assert all(r["steps"] >= 1 for r in rows)
    assert {r["sport"] for r in rows} == {"running", "strength"}


def test_marathon_plan_ends_on_race_day():
    plan, report = _marathon()
    last_date, last_spec = plan.items[-1]
    assert last_date == report["race"]["date"]
    assert last_spec.name.startswith("RACE — Test Marathon")


def test_every_week_has_two_strength_sessions():
    """The repo's central promise. Race week is the one exception, by design."""
    plan, report = _marathon()
    weeks = _weeks_of(plan)
    for idx, specs in sorted(weeks.items()):
        lifts = [s for s in specs if isinstance(s, StrengthSpec)]
        expected = 1 if idx == max(weeks) else 2
        assert len(lifts) == expected, f"week {idx + 1} has {len(lifts)} strength sessions"


def test_strength_never_lands_the_day_before_the_long_run():
    plan, _ = _marathon()
    by_date = {}
    for iso, spec in plan.items:
        by_date.setdefault(iso, []).append(spec)
    for iso, specs in by_date.items():
        if not any(isinstance(s, StrengthSpec) for s in specs):
            continue
        tomorrow = (date.fromisoformat(iso) + timedelta(days=1)).isoformat()
        assert not any(s.name.startswith("Long ") for s in by_date.get(tomorrow, []))


def test_every_session_carries_fuelling_guidance():
    plan, _ = _marathon()
    for _, spec in plan.items:
        text = (spec.description or "").lower()
        assert "fuel" in text or "recovery" in text or "movement quality" in text, spec.name


def test_plan_scales_to_the_runner_not_the_goal():
    """Same goal, different starting fitness: the low-mileage runner gets less volume
    and an explicit warning, not the same plan."""
    _, low = _marathon(goal="3:00:00", mpw=16, weeks=14)
    _, high = _marathon(goal="3:00:00", mpw=45, weeks=14)
    assert max(low["mileage_ramp"]) < max(high["mileage_ramp"])
    assert low["assessment"]["verdict"] == "out_of_reach"
    assert low["goal_built_toward"] != low["goal_requested"]
    assert low["assessment"]["gaps"]


def test_force_goal_overrides_the_recommendation():
    snap = summarize_fitness(_runs(16, today=TODAY), weeks=8, today=TODAY)
    race = MONDAY + timedelta(weeks=13, days=6)
    _, report = build_marathon_plan(
        race_name="Test Marathon", race_date=race.isoformat(), goal_time="3:00:00",
        snapshot=snap, start_date=MONDAY.isoformat(), today=TODAY, force_goal=True,
    )
    assert report["goal_built_toward"] == report["goal_requested"] == "3:00:00"


@pytest.mark.parametrize("weeks", [8, 12, 16, 20, 24])
def test_short_and_long_blocks_both_work(weeks):
    plan, report = _marathon(weeks=weeks)
    assert report["weeks"] == weeks
    phases = Counter(w["phase"] for w in report["weeks_detail"])
    assert phases["taper"] == 3          # the taper is never sacrificed
    assert phases["specific"] >= 1       # nor is specific work
    preview_plan(plan)


@pytest.mark.parametrize("dpw", [4, 5, 6])
def test_days_per_week_is_respected(dpw):
    snap = summarize_fitness(_runs(30, today=TODAY), weeks=8, today=TODAY)
    race = MONDAY + timedelta(weeks=15, days=6)
    plan, _ = build_marathon_plan(
        race_name="Test", race_date=race.isoformat(), goal_time="3:30:00",
        snapshot=snap, start_date=MONDAY.isoformat(), days_per_week=dpw, today=TODAY,
    )
    runs = [s for _, s in plan.items if not isinstance(s, StrengthSpec)]
    assert len(runs) >= dpw * 10


# --------------------------------------------------------------------------- #
# high school track
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("event,goal", [("800", "2:00"), ("1600", "4:30"),
                                        ("3200", "10:00"), ("5k", "15:00")])
def test_the_four_barrier_plans_build(event, goal):
    plan, report = build_track_plan(event=event, weeks=16,
                                    start_date=MONDAY.isoformat(), today=TODAY)
    assert report["goal"] == goal
    rows = preview_plan(plan)
    assert len(rows) == len(plan.items)
    assert all(r["steps"] >= 1 for r in rows)


def test_goal_splits_are_right():
    s = splits("800", "2:00")
    assert s["per_400"].startswith("60")
    assert splits("1600", "4:30")["per_400"].startswith("67")
    assert splits("3200", "10:00")["per_400"].startswith("75")


def test_sunday_is_always_a_rest_day():
    plan, _ = build_track_plan(event="3200", weeks=16,
                               start_date=MONDAY.isoformat(), today=TODAY)
    for iso, spec in plan.items:
        assert date.fromisoformat(iso).weekday() != 6, f"{spec.name} scheduled on a Sunday"


def test_track_plan_has_two_strength_sessions_a_week():
    plan, report = build_track_plan(event="1600", weeks=16,
                                    start_date=MONDAY.isoformat(), today=TODAY)
    weeks = _weeks_of(plan)
    for idx, specs in sorted(weeks.items())[:-1]:
        lifts = [s for s in specs if isinstance(s, StrengthSpec)]
        assert len(lifts) == 2, f"week {idx + 1} has {len(lifts)}"


def test_mileage_never_exceeds_the_age_ceiling():
    for event in MILEAGE_CEILING:
        for age, ceiling in MILEAGE_CEILING[event].items():
            _, report = build_track_plan(event=event, weeks=20, training_age=age,
                                         current_weekly_miles=ceiling * 0.9,
                                         start_date=MONDAY.isoformat(), today=TODAY)
            peak = max(w["target_miles"] for w in report["weeks_detail"])
            assert peak <= ceiling + 0.1, f"{event}/{age}: {peak} > {ceiling}"


def test_no_more_than_two_hard_days_plus_a_meet():
    meet = (MONDAY + timedelta(weeks=10, days=5)).isoformat()
    plan, _ = build_track_plan(event="1600", weeks=16, meet_days=[meet],
                               start_date=MONDAY.isoformat(), today=TODAY)
    weeks = _weeks_of(plan)
    for idx, specs in weeks.items():
        hard = [s for s in specs
                if not isinstance(s, StrengthSpec)
                and ("@" in s.name or s.name.startswith(("MEET", "TIME TRIAL", "tempo"))
                     or "tempo" in s.name)]
        assert len(hard) <= 3, f"week {idx + 1}: {[s.name for s in hard]}"


def test_track_gap_analysis_is_honest_about_multi_season_goals():
    _, report = build_track_plan(event="1600", weeks=14, current_pr="5:20",
                                 training_age="new", start_date=MONDAY.isoformat(),
                                 today=TODAY)
    a = report["assessment"]
    assert a["verdict"] == "multi_season"
    assert a["realistic_projection"] is not None
    assert any("season" in g for g in a["gaps"])


def test_reachable_track_goal_is_accepted():
    _, report = build_track_plan(event="1600", weeks=16, current_pr="4:38",
                                 training_age="developing",
                                 start_date=MONDAY.isoformat(), today=TODAY)
    assert report["assessment"]["verdict"] in ("on_track", "stretch")


def test_aerobic_reference_changes_the_paces():
    no_ref = event_paces("800", "2:00")
    with_ref = event_paces("800", "2:00", aerobic_reference=("3200", "11:30"))
    assert no_ref["P5K"] != with_ref["P5K"]
    assert with_ref["_aerobic_from_reference"] is True


# --------------------------------------------------------------------------- #
# nutrition / recovery
# --------------------------------------------------------------------------- #
def test_hs_nutrition_is_adequacy_only():
    """Non-negotiable: no calorie, macro-maths or weight prescription aimed at a minor.

    The one key excluded from the scan is the guardrail statement that names what
    the plan deliberately does NOT do — that sentence has to be able to say the
    words in order to rule them out.
    """
    brief = weekly_brief("build", 35, level="hs")
    nutrition = dict(brief["nutrition"])
    guardrail = nutrition.pop("what_this_plan_will_not_do")
    blob = str({**brief, "nutrition": nutrition}).lower()
    for banned in ("g/kg", "calorie", "deficit", "lose weight", "body fat",
                   "leaner", "weigh yourself", "cut back on food"):
        assert banned not in blob, f"HS brief prescribes {banned!r}"
    assert "enough" in blob
    # And the guardrail must still be there, saying so out loud.
    assert "calorie targets" in guardrail and "under-eating" in guardrail.lower()


def test_adult_brief_has_real_numbers():
    brief = weekly_brief("specific", 50, level="adult")
    assert "g/kg" in str(brief)
    assert "sleep" in str(brief["recovery"]).lower()


# --------------------------------------------------------------------------- #
# race catalogue
# --------------------------------------------------------------------------- #
def test_boston_is_patriots_day():
    """Third Monday in April, by Massachusetts law."""
    boston = races.BY_KEY["boston"]
    d = boston.occurrence(2027)
    assert d.weekday() == 0 and d.month == 4 and 15 <= d.day <= 21


def test_berlin_is_the_last_sunday_in_september():
    d = races.BY_KEY["berlin"].occurrence(2027)
    assert d.weekday() == 6 and d.month == 9 and d.day >= 24


def test_every_race_resolves_to_a_future_date_with_a_caveat():
    for row in races.list_races(after=TODAY):
        assert row["date"] > TODAY.isoformat()
        assert row["date_certainty"] in ("statutory", "established", "typical")
        assert "confirm" in row["caveat"].lower()


def test_random_race_respects_the_window_and_is_seedable():
    a = races.random_race(min_weeks_out=20, max_weeks_out=60, after=TODAY, seed=7)
    b = races.random_race(min_weeks_out=20, max_weeks_out=60, after=TODAY, seed=7)
    assert a == b
    assert 20 <= a["weeks_out"] <= 60


def test_find_race_matches_name_and_location():
    assert races.find_race("chicago", after=TODAY)["key"] == "chicago"
    assert races.find_race("Houston", after=TODAY)["key"] == "houston"
    assert races.find_race("not a real race", after=TODAY) is None


def test_random_race_can_drive_a_plan():
    """The 'surprise me' path end to end: pick a race, build the block for it."""
    race = races.random_race(min_weeks_out=20, max_weeks_out=52, after=TODAY, seed=3)
    snap = summarize_fitness(_runs(28, today=TODAY), weeks=8, today=TODAY)
    plan, report = build_marathon_plan(
        race_name=race["name"], race_date=race["date"], goal_time="3:45:00",
        snapshot=snap, today=TODAY,
    )
    assert plan.items[-1][0] == race["date"]
    assert report["assessment"]["weeks_available"] >= 19
