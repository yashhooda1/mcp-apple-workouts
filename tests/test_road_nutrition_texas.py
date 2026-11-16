"""Offline coverage for advanced adult road blocks, groceries and Texas races."""
from collections import Counter
from datetime import date

import pytest

from apple_workouts_mcp.nutrition import meal_plan
from apple_workouts_mcp.plans import preview_plan
from apple_workouts_mcp.road import build_road_plan
from apple_workouts_mcp.strength import StrengthSpec
from apple_workouts_mcp.texas_races import texas_race_calendar


@pytest.mark.parametrize("event,goal", [
    ("mile", "4:59"), ("5k", "16:59"), ("10k", "34:59"), ("half", "1:19:59"),
])
def test_road_block_compiles_and_ends_on_race_day(event, goal):
    plan, report = build_road_plan(
        event, goal_time=goal, weeks=10, start_date="2026-11-02",
        current_weekly_miles=35.0, days_per_week=5, today=date(2026, 10, 27),
    )
    rows = preview_plan(plan)
    assert len(rows) == len(plan.items)
    assert all(row["steps"] >= 1 for row in rows)
    assert plan.items[-1][0] == report["race"]["date"]
    assert plan.items[-1][1].name.startswith("RACE")
    assert any(isinstance(spec, StrengthSpec) for _, spec in plan.items)
    weeks = Counter((date.fromisoformat(day) - date(2026, 11, 2)).days // 7
                    for day, spec in plan.items if isinstance(spec, StrengthSpec))
    assert all(weeks[w] == 2 for w in range(9))
    assert weeks[9] <= 1


def test_benchmark_prevents_unearned_race_goal_training_pace():
    _, slow = build_road_plan(
        "5k", goal_time="16:59", benchmark_time="21:00", benchmark_distance="5k",
        weeks=8, start_date="2026-11-02", current_weekly_miles=30,
    )
    _, untested = build_road_plan(
        "5k", goal_time="16:59", weeks=8, start_date="2026-11-02",
        current_weekly_miles=30,
    )
    assert slow["training_pace_per_mile"] != untested["training_pace_per_mile"]
    assert "Benchmark" in slow["warning"]
    assert "No verified" in untested["warning"]


def test_ineligible_road_inputs_rejected():
    with pytest.raises(ValueError):
        build_road_plan("5k", weeks=6)
    with pytest.raises(ValueError):
        build_road_plan("half", current_weekly_miles=7)
    with pytest.raises(ValueError):
        build_road_plan("mile", benchmark_time="5:50")
    with pytest.raises(ValueError):
        build_road_plan("marathon")


@pytest.mark.parametrize("diet", ["vegan", "lacto_vegetarian", "vegetarian"])
def test_meals_and_grocery_categories(diet):
    result = meal_plan(diet=diet)
    assert len(result["days"]) == 7
    assert result["days"][-1]["run_fueling"]
    assert all(result["shopping_list"][k] for k in ("produce", "pantry", "refrigerated"))
    assert "B12" in result["nutrition"]["micronutrients"]
    foods = {x["item"] for items in result["shopping_list"].values() for x in items}
    if diet == "vegan":
        assert "paneer or shredded cheese" not in foods
        assert "plain Greek yogurt" not in foods
    else:
        assert "paneer or shredded cheese" in foods


def test_texas_calendar_city_distance_and_confirmed_dates():
    results = texas_race_calendar(area="Austin", distance="5K", after="2026-10-27")
    assert results["count"] >= 3
    assert all(r["area"] == "Austin" and "5K" in r["distances"]
               for r in results["races"])
    assert all(r["url"].startswith("https://") for r in results["races"])
    assert "austin_marathon" in {x["key"] for x in results["races"]}


def test_texas_calendar_never_invents_future_annual_dates():
    result = texas_race_calendar(after="2029-01-01", include_unscheduled=False)
    assert result["races"] == []


def test_road_plan_with_midweek_race_has_no_workouts_after_race():
    plan, report = build_road_plan(
        "5k", start_date="2026-11-02", race_date="2027-01-06",
        current_weekly_miles=35,
    )
    assert all(day <= report["race"]["date"] for day, _ in plan.items)


# ------------------------------------------------------------ 10K + base tier
def test_10k_has_its_own_sessions_and_aliases():
    plan, report = build_road_plan("10km", goal_time="45:00", weeks=10,
                                   start_date="2026-11-02", current_weekly_miles=28)
    names = [spec.name for _, spec in plan.items]
    assert report["tier"] == "advanced" and plan.family == "10K"
    assert any(n.startswith("10K intervals") for n in names)
    assert any(n.startswith("10K tempo") for n in names)
    assert plan.items[-1][1].name == "RACE — 10K (45:00)"
    assert report["goal_pace_per_mile"] == "7:15"


@pytest.mark.parametrize("event,goal,miles,days", [
    ("5k", "30:00", 8, 3), ("10k", "62:00", 12, 4), ("half", "2:20:00", 15, 4),
    ("mile", "8:30", 6, 3),
])
def test_base_tier_is_one_quality_day_and_fewer_days(event, goal, miles, days):
    plan, report = build_road_plan(event, goal_time=goal, weeks=10,
                                   start_date="2026-11-02", current_weekly_miles=miles)
    assert report["tier"] == "base" and report["days_per_week"] == days
    assert preview_plan(plan)                       # every session compiles
    assert plan.items[-1][1].name.startswith("RACE")
    start = date(2026, 11, 2)
    runs = [(date.fromisoformat(d), s) for d, s in plan.items
            if not isinstance(s, StrengthSpec)]
    for week in range(8):                           # build weeks, before the taper
        this = [s for d, s in runs if (d - start).days // 7 == week]
        assert len(this) == days
        hard = [s for s in this if "x" in s.name.split()[-1] or "(taper)" in s.name]
        assert len(hard) == 1, [s.name for s in this]
    # nothing in a low-mileage block asks for more than the half's own cap
    assert report["longest_run_miles"] <= 16


def test_base_tier_half_admits_the_long_run_is_short():
    _, report = build_road_plan("half", goal_time="2:20:00", weeks=10,
                                start_date="2026-11-02", current_weekly_miles=12)
    assert report["long_run_note"] and "run/walk" in report["long_run_note"]
    _, strong = build_road_plan("half", goal_time="1:35:00", weeks=12,
                                start_date="2026-11-02", current_weekly_miles=35)
    assert strong["long_run_note"] is None


def test_mileage_floor_and_day_limits():
    with pytest.raises(ValueError, match="5..120"):
        build_road_plan("5k", weeks=10, start_date="2026-11-02", current_weekly_miles=3)
    with pytest.raises(ValueError, match="4..7"):
        build_road_plan("5k", weeks=10, start_date="2026-11-02",
                        current_weekly_miles=30, days_per_week=3)
