"""iCalendar feed: event shape, folding, routes and the secret subscription URL."""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apple_workouts_mcp import server
from apple_workouts_mcp.feed import FeedStore
from apple_workouts_mcp.feed_http import CalendarSubscriptionRoutes, FeedRoutes
from apple_workouts_mcp.http_auth import BearerAuthMiddleware
from apple_workouts_mcp.ical import build_calendar, describe
from apple_workouts_mcp.plans import feed_item
from apple_workouts_mcp.strength import strength_b
from apple_workouts_mcp.workouts import easy, intervals


def _unfold(ics: str) -> str:
    return ics.replace("\r\n ", "")


def test_timed_running_event_has_paces_and_duration():
    item = feed_item("2026-11-10", intervals("Tempo 3x2mi", 3, "2mi", "6:15-6:25", "2:00"),
                     "06:00", "Aramco Half")
    ics = build_calendar([item])
    flat = _unfold(ics)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert f"UID:{item['id']}@hoodarun" in flat
    assert "DTSTART:20261110T060000" in flat and "DTEND:20261110T070000" in flat
    assert "6:15–6:25/mi" in flat and "3×" in flat and "Plan: Aramco Half" in flat
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_untimed_strength_is_all_day_with_exercise_names():
    item = feed_item("2026-11-12", strength_b("build"))
    flat = _unfold(build_calendar([item]))
    assert "DTSTART;VALUE=DATE:20261112" in flat and "DTEND;VALUE=DATE:20261113" in flat
    assert "Single Leg Hip Raise" in describe(item)


def test_since_drops_old_items_and_text_is_escaped():
    old = feed_item("2026-01-01", easy("Old, run; x", 3))
    new = feed_item("2026-12-01", easy("New, run; x", 3))
    flat = _unfold(build_calendar([old, new], since="2026-06-01"))
    assert "Old" not in flat and "New\\, run\\; x" in flat


def _app(store, cal_token="cal-secret"):
    async def mcp(_r):
        return PlainTextResponse("mcp")
    inner = FeedRoutes(Starlette(routes=[Route("/mcp", mcp)]), store)
    return CalendarSubscriptionRoutes(BearerAuthMiddleware(inner, "tok"), store, cal_token)


def test_routes(tmp_path):
    store = FeedStore(tmp_path)
    store.upsert([feed_item("2099-01-05", easy("Easy 6mi", 6), "06:30")])
    c = TestClient(_app(store))
    r = c.get("/calendar/cal-secret.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "Easy 6mi" in r.text
    assert c.get("/calendar/wrong.ics").status_code == 404
    assert c.get("/calendar.ics").status_code == 401           # header route stays gated
    assert c.get("/calendar.ics", headers={"Authorization": "Bearer tok"}).status_code == 200
    assert c.get("/mcp").status_code == 401                    # calendar secret != MCP access
    assert c.get("/mcp", headers={"Authorization": "Bearer cal-secret"}).status_code == 401


def test_subscription_route_off_without_token(tmp_path):
    c = TestClient(_app(FeedStore(tmp_path), cal_token=None))
    assert c.get("/calendar/None.ics").status_code == 404


def test_calendar_subscription_tool(monkeypatch):
    monkeypatch.delenv("CALENDAR_TOKEN", raising=False)
    assert server.calendar_subscription()["subscription_url"] is None
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://run.example.com/")
    monkeypatch.setenv("CALENDAR_TOKEN", "abc")
    out = server.calendar_subscription()
    assert out["subscription_url"] == "webcal://run.example.com/calendar/abc.ics"
