"""Strava rows feed the same fitness snapshot the plan builders use (offline)."""
from datetime import date, timedelta

import httpx
import pytest

from apple_workouts_mcp import strava
from apple_workouts_mcp.fitness import summarize_fitness

TODAY = date(2026, 10, 23)


def _activity(i, miles, sport="Run"):
    d = TODAY - timedelta(days=i)
    return {"id": i, "name": f"run {i}", "sport_type": sport,
            "start_date_local": f"{d.isoformat()}T06:30:00Z",
            "distance": miles * 1609.34, "moving_time": int(miles * 480),
            "average_heartrate": 150}


def test_rows_summarize():
    acts = [_activity(i, 6) for i in range(0, 56, 2)] + [_activity(1, 20, "Ride")]
    rows = [strava.to_row(a) for a in acts]
    snap = summarize_fitness(rows, weeks=8, today=TODAY).as_dict()
    assert 15 < snap["avg_weekly_miles"] < 25
    assert snap["longest_run_miles"] == pytest.approx(6.0, abs=0.1)


def test_refresh_and_paging(tmp_path, monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "s")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "seed")
    monkeypatch.setenv("STRAVA_TOKENS", str(tmp_path / "t.json"))
    pages = {1: [_activity(1, 5), _activity(3, 7)], 2: []}

    def handler(req: httpx.Request):
        if req.url.path == "/oauth/token":
            assert b"refresh_token=seed" in req.content
            return httpx.Response(200, json={"access_token": "A", "refresh_token": "R2",
                                             "expires_at": 9_999_999_999})
        assert req.headers["authorization"] == "Bearer A"
        return httpx.Response(200, json=pages[int(req.url.params["page"])])

    rows = strava.list_activities(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [r["id"] for r in rows] == [1, 3]
    assert "R2" in (tmp_path / "t.json").read_text()   # rotated token persisted


def test_not_configured(tmp_path, monkeypatch):
    for k in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STRAVA_TOKENS", str(tmp_path / "none.json"))
    with pytest.raises(strava.StravaNotConfigured):
        strava.access_token()
