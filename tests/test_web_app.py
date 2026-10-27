"""Regression tests for the authenticated browser dashboard."""
from datetime import date, timedelta

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.http_auth import BearerAuthMiddleware
from apple_workouts_mcp.web_app import WebRoutes, _sign, _verify, execute

TOKEN = "long-test-token"


def next_monday():
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


@pytest.fixture
def store(tmp_path):
    return FeedStore(tmp_path)


@pytest.fixture
def client(store):
    async def fallback(_request):
        return PlainTextResponse("mcp")
    inner = Starlette(routes=[Route("/mcp", fallback)])
    return TestClient(BearerAuthMiddleware(WebRoutes(inner, store), TOKEN))


def api(client, payload, origin=None):
    headers = {"Authorization": "Bearer " + TOKEN}
    if origin:
        headers["Origin"] = origin
    return client.post("/api/web", json=payload, headers=headers)


def road_request():
    return {"action": "preview", "kind": "road", "params": {
        "distance": "5k", "goal_time": "16:59",
        "start_date": next_monday().isoformat(), "weeks": 8,
        "current_weekly_miles": 35.0, "days_per_week": 5,
        "benchmark_time": "18:15", "benchmark_distance": "5k",
    }}


@pytest.mark.parametrize("path,mime", [
    ("/", "text/html"), ("/app", "text/html"),
    ("/app/", "text/html"), ("/app/app.css", "text/css"),
    ("/app/app.js", "text/javascript"),
])
def test_static_app_public_and_secure(client, path, mime):
    result = client.get(path)
    assert result.status_code == 200
    assert mime in result.headers["content-type"]
    assert "default-src 'none'" in result.headers["content-security-policy"]
    assert result.headers["x-content-type-options"] == "nosniff"


def test_frontend_contains_real_app_without_token_storage(client):
    html = client.get("/app").text
    js = client.get("/app/app.js").text
    for view in ("plans", "workouts", "strength", "nutrition", "races", "schedule"):
        assert f'id="view-{view}"' in html
    assert 'fetch("/api/web"' in js
    assert "localStorage" not in js and "sessionStorage" not in js


def test_api_and_mcp_stay_private(client):
    assert client.post("/api/web", json={"action": "overview"}).status_code == 401
    assert client.get("/mcp").status_code == 401
    assert api(client, {"action": "overview"}).status_code == 200


def test_preview_never_writes_and_publish_requires_matching_token(client, store):
    request = road_request()
    preview = api(client, request)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result["count"] > 20
    assert result["published"] is False
    assert store.load()["workouts"] == []
    publication = {**request, "action": "publish"}
    assert api(client, publication).status_code == 400
    modified = {**publication, "preview_token": result["preview_token"],
                "params": {**publication["params"], "goal_time": "17:30"}}
    assert api(client, modified).status_code == 400
    saved = api(client, {**publication, "preview_token": result["preview_token"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["published"] == result["count"]
    assert saved.json()["pending_watch_sync"] is True
    assert len(store.load()["workouts"]) == result["count"]
    overview = api(client, {"action": "overview"}).json()
    assert overview["upcoming_count"] == result["count"]
    assert overview["on_watch_count"] == 0


@pytest.mark.parametrize("rest", ["rest", "recovery"])
def test_5x1000_compiles_with_standing_or_jogging_rest(client, rest):
    request = {"action": "preview", "kind": "workout", "params": {
        "schedule_date": (date.today() + timedelta(days=2)).isoformat(),
        "spec": {"name": "5 x 1000m", "location": "outdoor", "steps": [
            {"kind": "warmup", "duration": "15:00"},
            {"repeat": 5, "steps": [
                {"kind": "run", "duration": "1000m", "target": "pace:5:40-5:55"},
                {"kind": rest, "duration": "2:00", "label": "Recovery"},
            ]},
            {"kind": "cooldown", "duration": "10:00"},
        ]},
    }}
    result = api(client, request)
    assert result.status_code == 200, result.text
    workout = result.json()["sessions"][0]["workout"]
    assert workout["blocks"][0]["iterations"] == 5
    assert workout["blocks"][0]["steps"][1]["purpose"] == "recovery"


def test_strength_preset_publishes(client, store):
    request = {"action": "preview", "kind": "strength", "params": {
        "schedule_date": next_monday().isoformat(), "preset": "b", "phase": "build"
    }}
    preview = api(client, request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["sessions"][0]["exercises"]
    published = api(client, {**request, "action": "publish",
                            "preview_token": preview.json()["preview_token"]})
    assert published.status_code == 200, published.text
    assert store.load()["workouts"][0]["sport"] == "strength"


def test_food_races_are_read_only(client, store):
    menu = api(client, {"action": "nutrition", "diet": "vegan", "servings": 1})
    assert menu.status_code == 200
    assert len(menu.json()["days"]) == 7
    races = api(client, {"action": "races", "area": "Austin", "distance": "5K"})
    assert races.status_code == 200
    assert all(row["area"] == "Austin" for row in races.json()["races"])
    assert store.load()["workouts"] == []


def test_origin_and_large_body_rejected(client):
    assert api(client, {"action": "overview"},
               origin="https://other.example").status_code == 403
    huge = api(client, {"action": "nutrition", "x": "A" * 140_000})
    assert huge.status_code == 413


def test_preview_signature_fails_for_mutated_content():
    value = {"kind": "road", "params": {"goal_time": "16:59"}}
    token = _sign(value)
    assert _verify(value, token)
    assert not _verify({"kind": "road", "params": {"goal_time": "17:00"}}, token)
    assert not _verify(value, "invalid")


def test_unreasonable_marathon_date_rejected_before_generation(store):
    monday = next_monday()
    request = {"action": "preview", "kind": "marathon", "params": {
        "race_name": "Distant race",
        "race_date": (monday + timedelta(days=3650)).isoformat(),
        "goal_time": "2:59:59", "current_weekly_miles": 35,
        "start_date": monday.isoformat(),
    }}
    with pytest.raises(ValueError, match="8 to 30 weeks"):
        execute(store, request)


def test_bad_input_returns_json_error(client):
    response = client.post("/api/web",
        headers={"Authorization": "Bearer " + TOKEN}, content=b"not-json")
    assert response.status_code == 400
    assert api(client, {"action": "nutrition", "servings": 99}).status_code == 400
    assert api(client, {"action": "nope"}).status_code == 400
