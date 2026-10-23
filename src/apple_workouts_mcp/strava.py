"""Read-only training history from Strava, through your own Strava API app.

Apple Health data never leaves the iPhone, but every Watch run already lands on
Strava through Strava's Apple Health sync — so Strava is where the server reads
history for `assess_fitness` and the plan builders. This talks to Strava's
public REST API directly with your own API application (no third-party
library), scope `activity:read_all`. Nothing here writes to Strava.

Setup (once):
  1. https://www.strava.com/settings/api — create an app, callback domain
     `localhost`. Put the id/secret in STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET.
  2. Run `apple-workouts-strava-auth` and follow the prompt. It stores the
     refresh token in the token file (STRAVA_TOKENS, default
     ~/.hooda-workouts/strava_tokens.json). Strava rotates refresh tokens, so
     the file is rewritten on every refresh — treat it like a password.

Without Strava configured, every tool still works: pass `current_weekly_miles`.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"


class StravaNotConfigured(RuntimeError):
    pass


def token_path() -> Path:
    return Path(os.environ.get("STRAVA_TOKENS",
                               "~/.hooda-workouts/strava_tokens.json")).expanduser()


def _client_creds() -> tuple[str, str]:
    cid, secret = os.environ.get("STRAVA_CLIENT_ID"), os.environ.get("STRAVA_CLIENT_SECRET")
    if not (cid and secret):
        raise StravaNotConfigured(
            "Strava isn't configured (STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET). "
            "Pass current_weekly_miles instead, or run apple-workouts-strava-auth."
        )
    return cid, secret


def _save(tokens: dict) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: tokens[k] for k in
                             ("access_token", "refresh_token", "expires_at")}))
    os.chmod(p, 0o600)


def _load() -> dict:
    p = token_path()
    if p.exists():
        return json.loads(p.read_text())
    seed = os.environ.get("STRAVA_REFRESH_TOKEN")
    if seed:
        return {"access_token": "", "refresh_token": seed, "expires_at": 0}
    raise StravaNotConfigured(
        f"No Strava tokens at {p}. Run apple-workouts-strava-auth first."
    )


def access_token(http: Optional[httpx.Client] = None) -> str:
    tokens = _load()
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 120:
        return tokens["access_token"]
    cid, secret = _client_creds()
    http = http or httpx.Client(timeout=20)
    r = http.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret,
                                   "grant_type": "refresh_token",
                                   "refresh_token": tokens["refresh_token"]})
    r.raise_for_status()
    fresh = r.json()
    _save(fresh)
    return fresh["access_token"]


def to_row(a: dict) -> dict:
    """Strava SummaryActivity -> the row shape fitness.summarize_fitness reads."""
    return {
        "id": a.get("id"),
        "name": a.get("name"),
        "type": a.get("sport_type") or a.get("type"),
        "start": a.get("start_date_local"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("moving_time") or a.get("elapsed_time"),
        "avg_hr": a.get("average_heartrate"),
    }


def list_activities(days: int = 70, limit: int = 200,
                    http: Optional[httpx.Client] = None) -> list[dict]:
    """Recent activities, newest first, as fitness rows."""
    http = http or httpx.Client(timeout=20)
    headers = {"Authorization": f"Bearer {access_token(http)}"}
    after = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        r = http.get(f"{API}/athlete/activities", headers=headers,
                     params={"after": after, "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(to_row(a) for a in batch)
        page += 1
    rows.sort(key=lambda x: x["start"] or "", reverse=True)
    return rows[:limit]


def interactive_auth() -> None:
    """`apple-workouts-strava-auth`: one-time OAuth code exchange."""
    cid, secret = _client_creds()
    url = (f"{AUTH_URL}?client_id={cid}&response_type=code"
           "&redirect_uri=http://localhost/exchange_token"
           "&approval_prompt=force&scope=activity:read_all")
    print("1. Open this URL, approve, and you'll land on a localhost page that won't load:")
    print(f"   {url}")
    redirected = input("2. Paste the full URL you were redirected to: ").strip()
    code = redirected.split("code=", 1)[-1].split("&", 1)[0]
    r = httpx.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret,
                                    "code": code, "grant_type": "authorization_code"},
                   timeout=20)
    r.raise_for_status()
    _save(r.json())
    print(f"Saved Strava tokens to {token_path()}")
