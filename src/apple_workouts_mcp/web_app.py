"""Browser dashboard and JSON API for HoodaRun.

Static UI is public (contains no athlete data); every /api/web call stays
behind the existing bearer-token middleware. The feed remains the single
source of truth for HoodaRun's iPhone -> WorkoutKit sync.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import date
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs

from pydantic import ValidationError

from .apple import compile_any, parse_spec, step_count
from .feed import FeedStore
from .nutrition import meal_plan
from .plans import TrainingPlan, feed_item, preview_plan, push_plan
from .road import build_road_plan
from .strength import mobility_reset, strength_a, strength_b
from .texas_races import texas_race_calendar

MAX_BODY = 128 * 1024
MAX_SESSIONS = 280
PREVIEW_TTL_SECONDS = 15 * 60
# A restart invalidates previews; tokens are never saved to disk.
_PREVIEW_SECRET = secrets.token_bytes(32)

PUBLIC_WEB_PATHS = {
    "/app": "index.html",
    "/app/": "index.html",
    "/app/app.css": "app.css",
    "/app/app.js": "app.js",
}
CONTENT_TYPES = {
    "index.html": b"text/html; charset=utf-8",
    "app.css": b"text/css; charset=utf-8",
    "app.js": b"text/javascript; charset=utf-8",
}
CSP = (
    b"default-src 'none'; script-src 'self'; style-src 'self'; "
    b"img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    b"form-action 'none'; frame-ancestors 'none'"
)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sign(value: dict) -> str:
    expires = int(time.time()) + PREVIEW_TTL_SECONDS
    payload = str(expires).encode() + b"." + _canonical(value)
    signature = hmac.new(_PREVIEW_SECRET, payload, hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _verify(value: dict, token: str) -> bool:
    try:
        raw_exp, signature = token.split(".", 1)
        expires = int(raw_exp)
        if expires < time.time() or expires > time.time() + PREVIEW_TTL_SECONDS + 30:
            return False
        expected = hmac.new(
            _PREVIEW_SECRET, raw_exp.encode() + b"." + _canonical(value), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError, AttributeError):
        return False


def _build(payload: dict) -> tuple[TrainingPlan | None, dict, dict | None]:
    """Compile once without writes. Recompile when publishing under a fresh token."""
    kind = payload.get("kind")
    p = payload.get("params")
    if not isinstance(p, dict):
        raise ValueError("params must be an object")
    if kind == "road":
        permitted = {
            "distance", "goal_time", "weeks", "start_date", "race_date",
            "current_weekly_miles", "days_per_week", "benchmark_time",
            "benchmark_distance", "heat_penalty_s_per_mile",
        }
        if set(p) - permitted:
            raise ValueError("Unknown road plan option")
        plan, report = build_road_plan(**p)
    elif kind == "marathon":
        from plans.marathon import build_marathon_plan

        permitted = {
            "race_name", "race_date", "goal_time", "start_date", "current_weekly_miles",
            "days_per_week", "heat_penalty",
        }
        if set(p) - permitted:
            raise ValueError("Unknown marathon plan option")
        if "current_weekly_miles" not in p:
            raise ValueError("Enter current weekly mileage before building a marathon block")
        plan, report = build_marathon_plan(**p)
        # No implicit Strava or server-side athlete history lookups from web.
    elif kind == "workout":
        if set(p) - {"schedule_date", "spec", "time"}:
            raise ValueError("Unknown workout option")
        when = date.fromisoformat(p["schedule_date"])
        if when < date.today():
            raise ValueError("Cannot publish a workout in the past")
        spec = parse_spec(p["spec"])
        item = feed_item(when.isoformat(), spec, time=p.get("time"))
        return None, {
            "name": spec.name, "sessions": [{
                "date": item["date"], "name": item["name"], "sport": item["sport"],
                "steps": step_count(item["workout"]), "workout": item["workout"],
            }],
            "count": 1,
        }, item
    elif kind == "strength":
        if set(p) - {"schedule_date", "preset", "phase"}:
            raise ValueError("Unknown strength option")
        when = date.fromisoformat(p["schedule_date"])
        if when < date.today():
            raise ValueError("Cannot publish a strength session in the past")
        phase = p.get("phase", "build")
        if phase not in ("base", "build", "specific", "taper"):
            raise ValueError("Unknown strength phase")
        preset = p.get("preset", "a")
        if preset not in ("a", "b", "mobility"):
            raise ValueError("Unknown strength preset")
        spec = {"a": strength_a, "b": strength_b,
                "mobility": mobility_reset}[preset](phase) if preset != "mobility" \
                else mobility_reset()
        item = feed_item(when.isoformat(), spec)
        return None, {
            "name": spec.name, "sessions": [{
                "date": item["date"], "name": item["name"], "sport": item["sport"],
                "steps": step_count(item["workout"]),
                "exercises": [x.model_dump(exclude_none=True) for x in spec.exercises],
            }],
            "count": 1,
        }, item
    else:
        raise ValueError("Unknown plan type")

    if len(plan.items) > MAX_SESSIONS:
        raise ValueError(f"Too many sessions; maximum is {MAX_SESSIONS}")
    sessions = preview_plan(plan)
    return plan, {
        "name": plan.name, "count": len(sessions),
        "sessions": sessions, "report": report,
    }, None


def execute(store: FeedStore, payload: dict) -> dict:
    """Dispatch authenticated requests. 'preview' never writes to the feed."""
    action = payload.get("action")
    if action == "overview":
        if set(payload) != {"action"}:
            raise ValueError("Unexpected overview parameters")
        status = store.status() or {}
        scheduled = {x.get("id"): x.get("rev") for x in status.get("scheduled", [])}
        entries = store.load()["workouts"]
        upcoming = []
        for w in entries:
            if w["date"] < date.today().isoformat():
                continue
            upcoming.append({
                "id": w["id"], "name": w["name"], "date": w["date"],
                "sport": w["sport"], "plan": w.get("plan"),
                "on_watch": scheduled.get(w["id"]) == w["rev"],
            })
        return {
            "count": len(entries), "upcoming_count": len(upcoming),
            "on_watch_count": sum(row["on_watch"] for row in upcoming),
            "last_sync": status.get("synced_at"),
            "upcoming": upcoming[:30],
            "notice": ("Feed entries are not automatically on your Watch. Open "
                       "HoodaRun on your paired iPhone to sync WorkoutKit."),
        }
    if action == "races":
        allowed = {"action", "area", "distance"}
        if set(payload) - allowed:
            raise ValueError("Unknown race filter")
        return texas_race_calendar(area=payload.get("area") or None,
                                   distance=payload.get("distance") or None)
    if action == "nutrition":
        allowed = {"action", "diet", "training_load", "servings", "long_run_day"}
        if set(payload) - allowed:
            raise ValueError("Unknown nutrition option")
        return meal_plan(
            diet=payload.get("diet", "lacto_vegetarian"),
            training_load=payload.get("training_load", "high"),
            servings=payload.get("servings", 1),
            long_run_day=payload.get("long_run_day", "Sunday"),
        )
    if action not in ("preview", "publish"):
        raise ValueError("Unknown API action")
    # Signed previews bind the complete request payload including spec and date.
    data = {"kind": payload.get("kind"), "params": payload.get("params")}
    plan, summary, item = _build(data)
    if action == "preview":
        return {**summary, "kind": data["kind"], "preview_token": _sign(data),
                "expires_in_seconds": PREVIEW_TTL_SECONDS, "published": False}
    if not _verify(data, payload.get("preview_token", "")):
        raise ValueError("Preview expired or changed. Preview again before publishing.")
    if plan is not None:
        written = push_plan(store, plan, replace=True)
        return {"name": plan.name, "published": written["published"],
                "removed_from_previous_push": written["removed_from_previous_push"],
                "pending_watch_sync": True}
    assert item is not None
    stored = store.upsert([item])
    return {"name": stored[0]["name"], "published": 1, "pending_watch_sync": True}


class WebRoutes:
    """Small dependency-free ASGI router layered before the MCP router."""

    def __init__(self, app: Any, store: FeedStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if path in PUBLIC_WEB_PATHS and method == "GET":
            filename = PUBLIC_WEB_PATHS[path]
            body = files("apple_workouts_mcp").joinpath("web_assets", filename).read_bytes()
            await self._respond(send, 200, body, CONTENT_TYPES[filename], static=True)
            return
        if path == "/api/web" and method == "POST":
            await self._api(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _api(self, scope, receive, send):
        # Browser requests carry a secret bearer header, not a cookie.
        # Reject cross-site calls even if a token is somehow leaked.
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin", b"").decode(errors="replace")
        host = headers.get(b"host", b"").decode(errors="replace")
        if origin and origin not in (f"https://{host}", f"http://{host}"):
            await self._json(send, 403, {"error": "Cross-origin requests are forbidden"})
            return
        body = bytearray()
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            if len(body) > MAX_BODY:
                await self._json(send, 413, {"error": "Request too large"})
                return
            if not msg.get("more_body", False):
                break
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            result = execute(self.store, payload)
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            await self._json(send, 400, {"error": str(exc)[:350]})
            return
        await self._json(send, 200, result)

    async def _json(self, send, code: int, data: dict):
        await self._respond(
            send, code, json.dumps(data, default=str, ensure_ascii=False).encode(),
            b"application/json; charset=utf-8",
        )

    @staticmethod
    async def _respond(send, code: int, body: bytes, content_type: bytes,
                       static: bool = False):
        headers = [(b"content-type", content_type), (b"cache-control", b"no-store"),
                   (b"x-content-type-options", b"nosniff"),
                   (b"referrer-policy", b"no-referrer")]
        if static:
            headers.append((b"content-security-policy", CSP))
        await send({"type": "http.response.start", "status": code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
