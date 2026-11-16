"""Completed workouts reported by HoodaRun: validation, storage, the HTTP
endpoint per account, and plans built from a runner's own watch data."""
import json
from datetime import date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from apple_workouts_mcp import completed, server
from apple_workouts_mcp import profile as rp
from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.feed_http import FeedRoutes
from apple_workouts_mcp.http_auth import BearerAuthMiddleware

TODAY = date.today()


def run(days_ago, miles, minutes, uid=None, **extra):
    start = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()) \
        .replace(hour=6)
    return {"id": uid or f"hk-{days_ago}-{miles}", "type": "running",
            "start": start.isoformat(), "distance_m": miles * 1609.34,
            "duration_s": minutes * 60, "avg_hr": 148, "source": "Apple Watch", **extra}


def month_of_running(per_week=(4, 6, 5, 10)):
    """Four weeks, four runs a week."""
    return [run(week * 7 + i * 2 + 1, miles, miles * 9, uid=f"r{week}{i}")
            for week in range(4) for i, miles in enumerate(per_week)]


# ----------------------------------------------------------------- the store
def test_normalize_keeps_good_rows_and_drops_bad_ones():
    row = completed.normalize(run(1, 5, 45, max_hr=171, elevation_gain_m=40.4))
    assert row["type"] == "Run" and row["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert row["distance_m"] == 8047 and row["duration_s"] == 2700 and row["max_hr"] == 171
    for bad in ({"id": "x"}, {**run(1, 5, 45), "start": "yesterday"},
                {**run(1, 5, 45), "duration_s": 3}, {**run(1, 5, 45), "id": ""},
                {**run(900, 5, 45)}, "not a dict"):
        assert completed.normalize(bad) is None
    odd = completed.normalize({**run(1, 5, 45), "avg_hr": 900, "distance_m": "NaN",
                               "type": "Underwater Hockey"})
    assert "avg_hr" not in odd and "distance_m" not in odd and odd["type"] == "Other"


def test_record_merges_by_id_and_reports(tmp_path):
    store = FeedStore(tmp_path)
    first = completed.record(store, [run(3, 5, 45, uid="a"), run(2, 4, 36, uid="b"), {"junk": 1}])
    assert (first["received"], first["accepted"], first["new"], first["stored"]) == (3, 2, 2, 2)
    again = completed.record(store, [run(2, 4.2, 37, uid="b"), run(1, 6, 54, uid="c")])
    assert (again["new"], again["stored"]) == (1, 3)
    rows = completed.load(store)
    assert [r["id"] for r in rows] == ["a", "b", "c"]                 # oldest first
    assert next(r for r in rows if r["id"] == "b")["distance_m"] == round(4.2 * 1609.34)
    assert [r["id"] for r in completed.recent(store, 2)] == ["b", "c"]


def test_view_shows_miles_and_pace():
    v = completed.view(completed.normalize(run(1, 5, 45, elevation_gain_m=30)))
    assert v["miles"] == 5.0 and v["pace_per_mile"] == "9:00" and v["avg_hr"] == 148
    assert v["climb_ft"] == 98 and v["minutes"] == 45.0
    lift = completed.view(completed.normalize({**run(1, 0, 40), "type": "strength",
                                               "distance_m": 0}))
    assert lift["type"] == "Strength" and "pace_per_mile" not in lift and "miles" not in lift


def test_a_file_written_by_the_app_over_icloud_is_read_and_cleaned(tmp_path):
    (tmp_path / "completed.json").write_text(json.dumps(
        {"schema": "hooda.workouts.completed/v1",
         "workouts": [run(1, 5, 45, uid="ok"), {"id": "broken"}]}))
    assert [r["id"] for r in completed.load(FeedStore(tmp_path))] == ["ok"]


# --------------------------------------------------------------- the endpoint
def test_post_completed_is_gated_and_validated(tmp_path):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    store = FeedStore(tmp_path)
    c = TestClient(BearerAuthMiddleware(FeedRoutes(inner, store), "tok"))
    h = {"Authorization": "Bearer tok"}
    assert c.post("/completed", json={"workouts": []}).status_code == 401
    assert c.post("/completed", json={"nope": 1}, headers=h).status_code == 400
    assert c.post("/completed", content=b"[1,2]", headers=h).status_code == 400
    assert c.post("/completed", content=b"x" * 300_000, headers=h).status_code == 413
    ok = c.post("/completed", json={"workouts": [run(1, 5, 45)]}, headers=h)
    assert ok.status_code == 200 and ok.json()["new"] == 1 and ok.json()["latest_start"]
    assert len(completed.load(store)) == 1


# ------------------------------------------------------------ through the tools
@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", lambda: FeedStore(tmp_path))
    monkeypatch.setattr(server, "_identity", lambda: None)
    return server


def test_list_completed_and_the_empty_note(tools):
    out = tools.list_completed()
    assert out["count"] == 0 and "Share completed workouts" in out["note"]
    completed.record(tools.store(), [run(1, 5, 45), run(30, 8, 70)])
    out = tools.list_completed(14)
    assert out["count"] == 1 and out["workouts"][0]["pace_per_mile"] == "9:00"
    assert "note" not in out and tools.list_completed(60)["count"] == 2
    with pytest.raises(ValueError):
        tools.list_completed(0)


def test_fitness_and_plans_come_from_the_runners_own_watch(tools):
    completed.record(tools.store(), month_of_running())               # 25 mi/week
    fit = tools.assess_fitness(weeks_of_history=4)
    assert fit["source"] == "watch" and 24 <= fit["fitness"]["avg_weekly_miles"] <= 26
    assert fit["fitness"]["longest_run_miles"] == 10.0

    plan = tools.create_road_race_plan("10k", goal_time="48:00", weeks=10,
                                       start_date="2026-11-16")
    assert 24 <= plan["report"]["from_watch"]["current_weekly_miles"] <= 26
    assert plan["report"]["tier"] == "advanced"

    marathon = tools.create_marathon_plan("Test Marathon", "2027-05-02", "4:00:00")
    assert marathon["report"]["history_source"] == "watch"


def test_measured_mileage_beats_a_saved_number_but_not_an_explicit_one(tools):
    rp.update(tools.store(), {"weekly_miles": 45})
    completed.record(tools.store(), month_of_running())
    measured = tools.create_road_race_plan("5k", weeks=10, start_date="2026-11-16")
    assert "from_watch" in measured["report"]
    assert "current_weekly_miles" not in measured["report"].get("from_profile", {})
    explicit = tools.create_road_race_plan("5k", weeks=10, start_date="2026-11-16",
                                           current_weekly_miles=40)
    assert "from_watch" not in explicit["report"]


def test_thin_history_falls_back_to_the_profile(tools):
    completed.record(tools.store(), [run(2, 4, 36), run(4, 5, 45)])   # two runs: not a history
    rp.update(tools.store(), {"weekly_miles": 22})
    plan = tools.create_road_race_plan("5k", weeks=10, start_date="2026-11-16")
    assert plan["report"]["from_profile"]["current_weekly_miles"] == 22
    assert "from_watch" not in plan["report"]


def test_a_member_without_history_is_told_how_to_get_one(tools, monkeypatch):
    class Member:
        is_owner = False
    monkeypatch.setattr(server, "_identity", lambda: Member())
    with pytest.raises(ValueError, match="Share completed workouts"):
        tools.assess_fitness()
    completed.record(tools.store(), month_of_running())
    assert tools.assess_fitness()["source"] == "watch"                # no Strava needed
