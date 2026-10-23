"""Feed round-trips: idempotent ids, revs, pruning, and the server tools."""
import asyncio
import json

import pytest

from apple_workouts_mcp import server
from apple_workouts_mcp.feed import FEED_SCHEMA, FeedStore, workout_id
from apple_workouts_mcp.feed_http import FeedRoutes
from apple_workouts_mcp.plans import TrainingPlan, push_plan
from apple_workouts_mcp.strength import strength_b
from apple_workouts_mcp.workouts import easy, intervals


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKOUT_FEED_DIR", str(tmp_path))
    return FeedStore()


def _plan(name="Aramco Half block", extra=True):
    items = [("2026-10-27", intervals("Tempo 3x2mi", 3, "2mi", "6:15-6:25", "2:00")),
             ("2026-10-27", strength_b("build")),
             ("2026-10-29", easy("Easy 6mi", 6))]
    if extra:
        items.append(("2026-11-01", easy("Easy 8mi", 8)))
    return TrainingPlan(name=name, items=items)


def test_ids_are_deterministic_and_uppercase():
    a = workout_id("2026-10-27", "Easy 6mi")
    assert a == workout_id("2026-10-27", "Easy 6mi") and a == a.upper()
    assert a != workout_id("2026-10-28", "Easy 6mi")


def test_push_is_idempotent(store):
    push_plan(store, _plan())
    first = store.load()
    push_plan(store, _plan())
    second = store.load()
    assert first["schema"] == FEED_SCHEMA
    assert len(second["workouts"]) == 4
    assert [w["rev"] for w in first["workouts"]] == [w["rev"] for w in second["workouts"]]
    assert [w["date"] for w in second["workouts"]] == sorted(w["date"] for w in second["workouts"])


def test_edited_plan_prunes_dropped_sessions_only(store):
    other = TrainingPlan(name="Other", items=[("2026-11-01", easy("Shakeout", 3))])
    push_plan(store, other)
    push_plan(store, _plan())
    out = push_plan(store, _plan(extra=False))
    assert [r["name"] for r in out["removed_from_previous_push"]] == ["Easy 8mi"]
    names = {w["name"] for w in store.load()["workouts"]}
    assert "Shakeout" in names and "Easy 8mi" not in names


def test_changed_content_changes_rev(store):
    push_plan(store, _plan())
    before = {w["name"]: w["rev"] for w in store.load()["workouts"]}
    push_plan(store, _plan(), time="06:00")
    after = {w["name"]: w["rev"] for w in store.load()["workouts"]}
    assert all(before[k] != after[k] for k in before)


def test_duplicate_session_same_day_is_rejected(store):
    plan = TrainingPlan(name="dup", items=[("2026-10-27", easy("Easy 6mi", 6))] * 2)
    with pytest.raises(ValueError, match="duplicate"):
        push_plan(store, plan)


def test_bad_date_and_time_rejected(store):
    with pytest.raises(ValueError):
        push_plan(store, TrainingPlan(name="x", items=[("2026-13-01", easy("E", 3))]))
    with pytest.raises(ValueError):
        push_plan(store, TrainingPlan(name="x", items=[("2026-10-01", easy("E", 3))]),
                  time="25:00")


def test_server_tools_round_trip(store):
    res = server.create_training_plan(
        name="My half plan",
        items=[{"date": "2026-10-28", "spec": {"name": "Tempo 5mi", "steps": [
                    {"kind": "warmup", "duration": "2mi", "target": "hr:2"},
                    {"kind": "run", "duration": "5mi", "target": "pace:6:20-6:30"},
                    {"kind": "cooldown", "duration": "1mi", "target": "hr:2"}]}},
               {"date": "2026-10-28", "spec": {"name": "Core", "exercises": [
                    {"category": "PLANK", "hold": "45s", "sets": 3}]}}],
    )
    assert res["published"] == 2
    listed = server.list_scheduled()
    assert listed["count"] == 2 and not any(w["on_watch"] for w in listed["workouts"])

    tempo = next(w for w in store.load()["workouts"] if w["name"] == "Tempo 5mi")
    store.write_status({"synced_at": "2026-10-23T18:00:00Z",
                        "scheduled": [{"id": tempo["id"], "rev": tempo["rev"]}]})
    on = {w["name"]: w["on_watch"] for w in server.list_scheduled()["workouts"]}
    assert on == {"Tempo 5mi": True, "Core": False}

    dry = server.clear_scheduled("2026-10-01", "2026-10-31")
    assert dry["dry_run"] and dry["found"] == 2 and store.load()["workouts"]
    server.unschedule_workout(tempo["id"].lower())
    assert [w["name"] for w in store.load()["workouts"]] == ["Core"]
    assert server.clear_scheduled("2026-10-01", "2026-10-31", dry_run=False)["removed"] == 1


def test_generated_marathon_plan_publishes(store):
    out = server.create_marathon_plan(
        race_name="Test", race_date="2027-04-18", goal_time="3:15:00",
        current_weekly_miles=35, start_date="2026-12-21", dry_run=False)
    assert out["published"] == len(store.load()["workouts"]) > 50


# ---------------------------------------------------------------- HTTP routes
async def _call(app, method, path, body=b""):
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app({"type": "http", "method": method, "path": path, "headers": []},
              receive, send)
    return sent[0]["status"], json.loads(sent[1]["body"])


def test_feed_routes(store):
    push_plan(store, _plan())
    fallthrough = []

    async def inner(scope, receive, send):
        fallthrough.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = FeedRoutes(inner, store)
    code, feed = asyncio.run(_call(app, "GET", "/feed.json"))
    assert code == 200 and len(feed["workouts"]) == 4
    code, _ = asyncio.run(_call(app, "POST", "/status", b'{"synced_at": "now"}'))
    assert code == 200 and store.status() == {"synced_at": "now"}
    code, _ = asyncio.run(_call(app, "POST", "/status", b"[1]"))
    assert code == 400
    asyncio.run(_call(app, "POST", "/mcp"))
    assert fallthrough == ["/mcp"]
