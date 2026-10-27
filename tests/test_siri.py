"""Offline Siri bridge tests: feed publishing, auth, validation and no writes on preview."""
from datetime import date, timedelta

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.http_auth import BearerAuthMiddleware
from apple_workouts_mcp.siri import SiriRequestError, SiriRoutes, dispatch

TOKEN = "private-test-token"


@pytest.fixture()
def store(tmp_path):
    return FeedStore(tmp_path)


def run_options(kind="easy", **changes):
    result = {"date": (date.today() + timedelta(days=1)).isoformat(), "kind": kind}
    if kind == "easy":
        result["miles"] = 6.0
    else:
        result.update({
            "repeats": 5, "work_meters": 1000,
            "pace_fast": "5:40", "pace_slow": "5:55",
            "recovery_seconds": 120, "recovery_style": "jog",
        })
    result.update(changes)
    return result


def test_read_only_voice_actions(store):
    next_run = dispatch(store, {"action": "next_workout"})
    assert next_run["found"] is False
    races = dispatch(store, {"action": "texas_races", "area": "Austin"})
    assert all(race["area"] == "Austin" for race in races["races"])
    meals = dispatch(store, {"action": "meal_ideas", "diet": "vegan"})
    assert "plant-based" in meals["message"]
    assert store.load()["workouts"] == []


def test_preview_requires_matching_token_then_publishes(store):
    options = run_options()
    preview = dispatch(store, {"action": "preview_run", "options": options})
    assert preview["name"] == "Siri Easy 6mi"
    assert not store.load()["workouts"]
    with pytest.raises(SiriRequestError, match="Preview expired"):
        dispatch(store, {"action": "publish_run", "options": options})
    changed = {**options, "miles": 7}
    with pytest.raises(SiriRequestError, match="Preview expired"):
        dispatch(store, {"action": "publish_run", "options": changed,
                         "preview_token": preview["preview_token"]})
    result = dispatch(store, {"action": "publish_run", "options": options,
                              "preview_token": preview["preview_token"]})
    assert result["published"] is True
    workouts = store.load()["workouts"]
    assert len(workouts) == 1
    assert workouts[0]["workout"]["activity"] == "running"
    response = dispatch(store, {"action": "next_workout"})
    assert response["found"] is True and response["on_watch"] is False
    assert "not yet confirmed" in response["message"].lower()
    store.write_status({"scheduled": [{"id": workouts[0]["id"], "rev": workouts[0]["rev"]}]})
    assert dispatch(store, {"action": "next_workout"})["on_watch"] is True


@pytest.mark.parametrize("style", ["jog", "standing"])
def test_interval_compiler_and_rest_modes(store, style):
    options = run_options("intervals", recovery_style=style)
    preview = dispatch(store, {"action": "preview_run", "options": options})
    dispatch(store, {"action": "publish_run", "options": options,
                     "preview_token": preview["preview_token"]})
    session = store.load()["workouts"][0]["workout"]
    assert session["blocks"][0]["iterations"] == 5
    assert session["blocks"][0]["steps"][0]["step"]["goal"]["meters"] == 1000.0
    recovery = session["blocks"][0]["steps"][1]
    assert recovery["purpose"] == "recovery"
    assert recovery["step"]["display_name"] == (
        "Standing rest" if style == "standing" else "Easy jog"
    )
    if style == "standing":
        assert recovery["step"]["alert"] is None
    else:
        assert recovery["step"]["alert"]["type"] == "hr_zone"


@pytest.mark.parametrize("bad", [
    {"miles": -4}, {"miles": "NaN"}, {"miles": 300},
    {"date": "1900-01-01"}, {"date": "2050-01-01"},
    {"unexpected": "code"},
])
def test_invalid_easy_runs_rejected(store, bad):
    with pytest.raises((SiriRequestError, ValueError)):
        dispatch(store, {"action": "preview_run", "options": run_options(**bad)})
    assert store.load()["workouts"] == []


@pytest.mark.parametrize("bad", [
    {"repeats": 0}, {"repeats": 50}, {"work_meters": 9},
    {"recovery_seconds": 900}, {"pace_fast": "bad"},
    {"pace_fast": "8:00", "pace_slow": "6:00"},
    {"recovery_style": "arbitrary"}, {"pace_fast": "2:30"},
])
def test_invalid_intervals_rejected(store, bad):
    with pytest.raises((SiriRequestError, ValueError)):
        dispatch(store, {"action": "preview_run",
                         "options": run_options("intervals", **bad)})


def test_http_route_is_private_and_limits_payload(store):
    async def fallback(_request):
        return PlainTextResponse("ok")

    app = BearerAuthMiddleware(
        SiriRoutes(Starlette(routes=[Route("/mcp", fallback)]), store),
        TOKEN,
    )
    with TestClient(app) as client:
        assert client.post("/api/siri", json={"action": "next_workout"}).status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.post("/api/siri", json={"action": "next_workout"},
                               headers=headers)
        assert response.status_code == 200
        assert response.json()["found"] is False
        assert client.get("/api/siri", headers=headers).status_code == 405
        assert client.post("/api/siri", content=b"not json",
                           headers=headers).status_code == 400
        assert client.post("/api/siri", json={"action": "meal_ideas", "extra": "x" * 13_000},
                           headers=headers).status_code == 413
        assert client.post("/api/siri", json={"action": "bad"},
                           headers=headers).status_code == 400
        assert client.post("/api/siri", json={"action": "next_workout", "extra": "x"},
                           headers=headers).status_code == 400
