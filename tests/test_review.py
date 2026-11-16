"""Reviewing a week against what was run, and adjusting what's ahead."""
from datetime import date, datetime, timedelta

import pytest

from apple_workouts_mcp import completed, review
from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.plans import push_plan
from apple_workouts_mcp.road import build_road_plan

MON = date(2026, 11, 16)
TODAY = MON + timedelta(days=6)            # Sunday of week 1


@pytest.fixture
def store(tmp_path):
    s = FeedStore(tmp_path)
    plan, _ = build_road_plan("10k", goal_time="45:00", weeks=8, start_date=MON.isoformat(),
                              current_weekly_miles=28)
    push_plan(s, plan, time="06:00")
    return s


def did(day, miles, minutes=None, kind="running", uid=None):
    start = datetime.combine(day, datetime.min.time()).replace(hour=6)
    return {"id": uid or f"hk-{day}-{kind}-{miles}", "type": kind, "start": start.isoformat(),
            "distance_m": miles * 1609.34, "duration_s": (minutes or miles * 9) * 60,
            "avg_hr": 150}


def planned(store, day):
    return [w for w in store.between(day.isoformat(), day.isoformat())]


def report(store, rows, monkeypatch, today=TODAY, week_of=None):
    monkeypatch.setattr(completed, "normalize", _normalize_any_age)
    completed.record(store, rows)
    return review.review_week(store, week_of, today=today)


_real_normalize = completed.normalize


def _normalize_any_age(raw):
    """The fixture dates are fixed; don't let 'too old' drop them as time passes."""
    row = _real_normalize({**raw, "start": datetime.now().isoformat()}) if isinstance(raw, dict) \
        else None
    if row and isinstance(raw, dict) and raw.get("start"):
        start = datetime.fromisoformat(raw["start"])
        row["start"], row["date"] = start.isoformat(timespec="seconds"), start.date().isoformat()
    return row


# ------------------------------------------------------------------ what's planned
def test_planned_miles_and_kinds(store):
    rows = store.between("2026-11-16", "2026-11-22")
    week = {w["name"]: w for w in rows}
    kinds = [review.kind_of(w) for w in rows]
    assert review.kind_of(week["10K intervals 4x1mi"]) == "quality"
    assert review.kind_of(week["10K tempo 2x2mi"]) == "quality"
    assert sorted(kinds) == ["easy", "easy", "long", "quality", "quality",
                             "strength", "strength"]
    easy = next(w for n, w in week.items() if n.startswith("Easy"))
    assert review.planned_miles(easy["workout"]) == pytest.approx(float(easy["name"][5:-2]), 0.01)
    assert review.planned_miles(week["10K intervals 4x1mi"]["workout"]) == pytest.approx(6.5, 0.01)


# ----------------------------------------------------------------------- review
def test_a_week_done_as_planned(store, monkeypatch):
    rows = [did(date.fromisoformat(w["date"]), review.planned_miles(w["workout"]) + 0.3)
            if w["sport"] == "running" else
            did(date.fromisoformat(w["date"]), 0, 32, kind="strength", uid="s" + w["date"])
            for w in store.between("2026-11-16", "2026-11-22")]
    out = report(store, rows, monkeypatch)
    s = out["summary"]
    assert {x["status"] for x in out["sessions"]} == {"done"}
    assert s["quality_sessions"] == "2 of 2" and s["strength_sessions"] == "2 of 2"
    assert s["long_run"] == "done" and not s["missed"] and out["unplanned"] == []
    assert out["reading"]["verdict"].startswith("on plan") and out["reading"]["suggest"] is None
    one = next(x for x in out["sessions"] if x["kind"] == "long")
    assert one["actual"]["pace_per_mile"] and one["actual"]["avg_hr"] == 150


def test_a_week_that_fell_apart_suggests_easing_not_cramming(store, monkeypatch):
    monday_run = planned(store, MON)[0]
    out = report(store, [did(MON, review.planned_miles(monday_run["workout"])),
                         did(MON + timedelta(days=1), 2.0)], monkeypatch,
                 today=MON + timedelta(days=7), week_of=MON.isoformat())   # looking back
    assert not out["in_progress"]
    s = out["summary"]
    assert s["quality_sessions"] == "1 of 2" and s["long_run"] == "missed"
    assert any("tempo" in m for m in s["missed"]) and any("intervals" in c for c in s["cut_short"])
    assert out["reading"]["suggest"] == {"action": "ease", "factor": 0.8, "days": 7}
    assert "Don't make up missed runs" in out["reading"]["why"]


def test_midweek_review_does_not_call_future_sessions_missed(store, monkeypatch):
    wed = MON + timedelta(days=2)
    rows = [did(MON, 7.0), did(MON + timedelta(days=1), 6.6), did(wed, 3.1, uid="bonus")]
    out = report(store, rows, monkeypatch, today=wed)
    assert out["in_progress"]
    statuses = {x["name"]: x["status"] for x in out["sessions"]}
    assert statuses["10K tempo 2x2mi"] == "upcoming" and "missed" not in statuses.values() \
        or list(statuses.values()).count("missed") == 1      # Tuesday's lift only
    assert [u["id"] for u in out["unplanned"]] == ["bonus"]
    assert out["summary"]["planned_miles_so_far"] < out["summary"]["planned_miles"]


def test_overdoing_it_is_called_out(store, monkeypatch):
    rows = [did(MON + timedelta(days=d), 9.0, uid=f"x{d}") for d in range(7)]
    out = report(store, rows, monkeypatch)
    assert out["reading"]["verdict"].startswith("more than planned")
    assert out["reading"]["suggest"] is None and "injury" in out["reading"]["why"]


def test_review_without_any_reported_workouts_says_so(store):
    out = review.review_week(store, today=TODAY)
    assert "Share completed workouts" in out["note"] and len(out["planned"]) == 7
    assert "summary" not in out


# ----------------------------------------------------------------------- adjust
NEXT = MON + timedelta(days=7)             # Monday of week 2


def test_ease_turns_workouts_into_easy_runs_and_shortens_the_rest(store):
    before = {w["id"]: w for w in store.between("2026-11-23", "2026-11-29")}
    preview = review.adjust(store, "ease", NEXT.isoformat(), today=MON)
    assert preview["dry_run"] and store.between("2026-11-23", "2026-11-29") == list(before.values())
    assert preview["miles_after"] < preview["miles_before"] * 0.85

    out = review.adjust(store, "ease", NEXT.isoformat(), dry_run=False, today=MON)
    after = {w["id"]: w for w in store.between("2026-11-23", "2026-11-29")}
    assert set(after) == set(before)                           # same ids: events move in place
    runs = [w for w in after.values() if w["sport"] == "running"]
    assert all(review.kind_of(w) != "quality" for w in runs)
    assert all(w["rev"] != before[w["id"]]["rev"] for w in runs)   # the Watch will update
    eased = next(w for w in runs if "instead of" in w["name"])
    assert eased["adjusted"]["was"].startswith("10K") and eased["adjusted"]["action"] == "ease"
    long_before = next(w for w in before.values() if w["name"].startswith("Long"))
    long_after = after[long_before["id"]]
    assert review.planned_miles(long_after["workout"]) == pytest.approx(
        review.planned_miles(long_before["workout"]) * 0.8, abs=0.06)
    assert long_after["name"] != long_before["name"] and long_after["name"].startswith("Long easy")
    lifts = [w for w in after.values() if w["sport"] == "strength"]
    assert all(w == before[w["id"]] for w in lifts) and len(out["changed"]) == len(runs)
    # the week after is untouched
    assert all("adjusted" not in w for w in store.between("2026-11-30", "2026-12-06"))


def test_scale_changes_volume_but_leaves_workouts_alone(store):
    out = review.adjust(store, "scale", NEXT.isoformat(), factor=1.1, dry_run=False, today=MON)
    names = {c["was"] for c in out["changed"]}
    assert not any("intervals" in n or "tempo" in n for n in names)
    assert {u["name"] for u in out["unchanged"]} >= {"10K tempo 2x2mi"}
    assert out["miles_after"] > out["miles_before"]


def test_repace_moves_quality_paces_for_the_rest_of_the_plan(store):
    def paces(w):
        return [s["step"]["alert"] for b in w["workout"]["blocks"] for s in b["steps"]
                if (s["step"].get("alert") or {}).get("type") == "speed"]
    quality = [w for w in store.load()["workouts"] if review.kind_of(w) == "quality"]
    before = {w["id"]: paces(w)[0]["max_mps"] for w in quality}
    out = review.adjust(store, "repace", seconds_per_mile=10, dry_run=False, today=MON)
    after = {w["id"]: w for w in store.load()["workouts"]}
    assert len(out["changed"]) == len(quality)
    assert all(paces(after[i])[0]["max_mps"] < v for i, v in before.items())   # slower
    assert out["range"][1] > "2027-01-10"                      # ran to the end of the plan
    race = next(w for w in after.values() if w["name"].startswith("RACE"))
    assert "adjusted" not in race


def test_adjust_guards(store):
    for kwargs, why in [({"action": "taper"}, "action must be"),
                        ({"action": "ease", "factor": 0.1}, "between 0.4 and 1.0"),
                        ({"action": "scale"}, "needs a factor"),
                        ({"action": "scale", "factor": 1.5}, "between 0.5 and 1.15"),
                        ({"action": "repace"}, "seconds_per_mile"),
                        ({"action": "repace", "seconds_per_mile": -45}, "seconds_per_mile"),
                        ({"action": "ease", "start_date": "2026-11-01"}, "in the past")]:
        with pytest.raises(ValueError, match=why):
            review.adjust(store, today=MON, **kwargs)


def test_republishing_restores_and_a_move_survives_an_adjustment(store):
    from apple_workouts_mcp import reschedule

    target = next(w for w in store.between("2026-11-23", "2026-11-29")
                  if w["name"].startswith("Long"))
    reschedule.move(store, target["id"], "2026-11-28")
    review.adjust(store, "ease", NEXT.isoformat(), dry_run=False, today=MON)
    moved = next(w for w in store.load()["workouts"] if w["id"] == target["id"])
    assert moved["date"] == "2026-11-28" and moved["moved_from"] == "2026-11-29"
    assert moved["adjusted"]["action"] == "ease"

    plan, _ = build_road_plan("10k", goal_time="45:00", weeks=8, start_date=MON.isoformat(),
                              current_weekly_miles=28)
    push_plan(store, plan, time="06:00")
    again = next(w for w in store.load()["workouts"] if w["id"] == target["id"])
    assert "adjusted" not in again and again["name"] == target["name"]
    assert again["date"] == "2026-11-28"                       # the move is still kept
