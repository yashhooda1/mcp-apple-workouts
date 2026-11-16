"""Moving published workouts: the id stays, the Watch notices, re-publishing
doesn't undo it, and race day is treated as race day."""
from datetime import date

import pytest
from icalendar import Calendar

from apple_workouts_mcp import reschedule
from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.ical import build_calendar
from apple_workouts_mcp.plans import TrainingPlan, push_plan
from apple_workouts_mcp.road import build_road_plan
from apple_workouts_mcp.workouts import easy

TODAY = date(2026, 11, 16)   # a Monday


def _plan(name="Block", **sessions):
    return TrainingPlan(name=name, items=[(d, easy(n, 4)) for n, d in sessions.items()])


@pytest.fixture
def store(tmp_path):
    s = FeedStore(tmp_path)
    push_plan(s, _plan(Tue="2026-11-17", Thu="2026-11-19", Sun="2026-11-22"), time="06:00")
    return s


def _by_name(store):
    return {w["name"]: w for w in store.load()["workouts"]}


# ------------------------------------------------------------------- move
def test_move_keeps_the_id_and_changes_the_rev(store):
    before = _by_name(store)["Tue"]
    out = reschedule.move(store, before["id"].lower(), "2026-11-18")
    after = _by_name(store)["Tue"]
    assert out["moved"] and out["from"] == "2026-11-17"
    assert after["id"] == before["id"] and after["date"] == "2026-11-18"
    assert after["rev"] != before["rev"]            # HoodaRun reschedules on rev change
    assert after["moved_from"] == "2026-11-17" and after["time"] == "06:00"
    assert out["workout"]["planned_date"] == "2026-11-17"
    assert len(store.load()["workouts"]) == 3       # moved, not duplicated


def test_move_back_is_as_if_it_never_moved(store):
    before = _by_name(store)["Tue"]
    reschedule.move(store, before["id"], "2026-11-18")
    reschedule.move(store, before["id"], "2026-11-17")
    after = _by_name(store)["Tue"]
    assert after["rev"] == before["rev"] and "moved_from" not in after


def test_move_reports_what_else_is_on_that_day_and_can_set_a_time(store):
    w = _by_name(store)
    out = reschedule.move(store, w["Tue"]["id"], "2026-11-19", time="17:30")
    assert [o["name"] for o in out["same_day"]] == ["Thu"]
    assert _by_name(store)["Tue"]["time"] == "17:30"
    assert not reschedule.move(store, w["Tue"]["id"], "2026-11-19")["moved"]   # no-op


def test_move_rejects_bad_input(store):
    w = _by_name(store)["Tue"]
    with pytest.raises(ValueError, match="no scheduled workout"):
        reschedule.move(store, "not-an-id", "2026-11-18")
    with pytest.raises(ValueError, match="bad date"):
        reschedule.move(store, w["id"], "next thursday")
    with pytest.raises(ValueError, match="bad time"):
        reschedule.move(store, w["id"], "2026-11-18", time="6pm")


def test_the_calendar_event_moves_instead_of_duplicating(store):
    def events():
        cal = Calendar.from_ical(build_calendar(store.load()["workouts"]))
        return {str(e["UID"]): e["DTSTART"].dt for e in cal.walk("VEVENT")}

    before = events()
    reschedule.move(store, _by_name(store)["Tue"]["id"], "2026-11-20")
    after = events()
    assert set(after) == set(before)
    changed = [uid for uid in after if after[uid] != before[uid]]
    assert len(changed) == 1 and after[changed[0]].date() == date(2026, 11, 20)


# ------------------------------------------------------------------- swap
def test_swap_trades_days(store):
    w = _by_name(store)
    reschedule.swap(store, w["Thu"]["id"], w["Sun"]["id"])
    after = _by_name(store)
    assert (after["Thu"]["date"], after["Sun"]["date"]) == ("2026-11-22", "2026-11-19")
    assert after["Thu"]["moved_from"] == "2026-11-19"
    with pytest.raises(ValueError, match="two different"):
        reschedule.swap(store, w["Thu"]["id"], w["Thu"]["id"])


# ------------------------------------------------- moves survive re-publishing
def test_republishing_keeps_a_moved_session_where_the_runner_put_it(store):
    moved_id = _by_name(store)["Tue"]["id"]
    reschedule.move(store, moved_id, "2026-11-18", time="18:00")
    rev = _by_name(store)["Tue"]["rev"]

    out = push_plan(store, _plan(Tue="2026-11-17", Thu="2026-11-19", Sun="2026-11-22"),
                    time="06:00")
    after = _by_name(store)
    assert out["kept_moves"] == [{"name": "Tue", "planned_date": "2026-11-17",
                                  "date": "2026-11-18"}]
    assert after["Tue"]["date"] == "2026-11-18" and after["Tue"]["time"] == "18:00"
    assert after["Tue"]["rev"] == rev               # unchanged content: no Watch churn
    assert after["Thu"]["date"] == "2026-11-19" and len(after) == 3


def test_republishing_changed_content_still_updates_a_moved_session(store):
    reschedule.move(store, _by_name(store)["Tue"]["id"], "2026-11-18")
    rev = _by_name(store)["Tue"]["rev"]
    longer = TrainingPlan(name="Block", items=[("2026-11-17", easy("Tue", 6)),
                                               ("2026-11-19", easy("Thu", 4)),
                                               ("2026-11-22", easy("Sun", 4))])
    push_plan(store, longer, time="06:00")
    after = _by_name(store)["Tue"]
    assert after["date"] == "2026-11-18" and after["rev"] != rev
    assert "6" in str(after["spec"])


# ------------------------------------------------------------------ shift
def test_shift_is_a_dry_run_by_default(store):
    out = reschedule.shift(store, 7, today=TODAY)
    assert out["dry_run"] and out["shifted"] == 3
    assert out["first"]["date"] == "2026-11-24" and out["last"]["date"] == "2026-11-29"
    assert _by_name(store)["Tue"]["date"] == "2026-11-17"       # nothing written


def test_shift_a_window_and_report_doubled_days(store):
    out = reschedule.shift(store, 2, start_date="2026-11-17", end_date="2026-11-17",
                           dry_run=False, today=TODAY)
    assert out["shifted"] == 1 and _by_name(store)["Tue"]["date"] == "2026-11-19"
    assert out["doubled_days"] == [{"date": "2026-11-19", "runs": ["Thu", "Tue"]}] \
        or out["doubled_days"] == [{"date": "2026-11-19", "runs": ["Tue", "Thu"]}]


def test_shift_only_the_named_plan(store):
    push_plan(store, _plan("Other", Extra="2026-11-18"))
    reschedule.shift(store, 7, plan="other", dry_run=False, today=TODAY)
    after = _by_name(store)
    assert after["Extra"]["date"] == "2026-11-25" and after["Tue"]["date"] == "2026-11-17"


def test_shift_earlier_but_never_into_the_past(store):
    reschedule.shift(store, -1, dry_run=False, today=TODAY)
    assert _by_name(store)["Tue"]["date"] == "2026-11-16"
    with pytest.raises(ValueError, match="in the past"):
        reschedule.shift(store, -1, today=TODAY)
    for bad in (0, 500, 1.5, True):
        with pytest.raises(ValueError, match="whole number"):
            reschedule.shift(store, bad, today=TODAY)


def test_shifting_a_race_plan_keeps_race_day_and_warns(tmp_path):
    s = FeedStore(tmp_path)
    plan, _ = build_road_plan("5k", goal_time="21:00", weeks=8, start_date="2026-11-16",
                              current_weekly_miles=25)
    push_plan(s, plan)
    race = next(w for w in s.load()["workouts"] if reschedule.is_race(w))

    out = reschedule.shift(s, 7, today=TODAY)
    assert [w["id"] for w in out["race_days_kept"]] == [race["id"]]
    assert out["now_on_or_after_race_day"] and "clear the days you missed" in out["warning"]
    assert all(w["date"] >= race["date"] for w in out["now_on_or_after_race_day"])

    # the race itself was postponed: everything moves together, nothing overruns
    moved = reschedule.shift(s, 7, move_race=True, dry_run=False, today=TODAY)
    assert not moved["now_on_or_after_race_day"] and "warning" not in moved
    after = {w["id"]: w for w in s.load()["workouts"]}
    assert after[race["id"]]["date"] == "2027-01-17" != race["date"]
    assert moved["shifted"] == len(after)

    # and publishing the same plan again leaves the shifted block where it is
    again = push_plan(s, plan)
    assert len(again["kept_moves"]) == len(after)
    assert {w["id"]: w["date"] for w in s.load()["workouts"]} == \
        {i: w["date"] for i, w in after.items()}
