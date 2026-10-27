"""Private Siri/App Intents -> existing MCP workout-feed bridge.

Siri is NOT a native MCP transport. A paired iPhone executes App Intents
and makes authenticated calls here. Run publishing requires a signed preview
plus confirmation on device. No general-purpose LLM tool execution endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
from datetime import date, timedelta

from .feed import FeedStore
from .nutrition import meal_plan
from .plans import feed_item
from .texas_races import texas_race_calendar
from .workouts import RepeatSpec, StepSpec, WorkoutSpec

MAX_VOICE_BODY = 12 * 1024
TOKEN_LIFETIME = 10 * 60
_SECRET = secrets.token_bytes(32)
_CLOCK = re.compile(r"^[0-9]{1,2}:[0-5][0-9]$")


class SiriRequestError(ValueError):
    """A safe validation error for spoken/shortcut requests."""


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")


def _token(data: dict) -> str:
    expires = int(time.time()) + TOKEN_LIFETIME
    body = f"{expires}.".encode() + _canonical(data)
    mac = hmac.new(_SECRET, body, hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def _valid_token(data: dict, token: str) -> bool:
    try:
        expiry, mac = token.split(".", 1)
        deadline = int(expiry)
        now = time.time()
        if deadline < now or deadline > now + TOKEN_LIFETIME + 5:
            return False
        expected = hmac.new(
            _SECRET, expiry.encode() + b"." + _canonical(data), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(mac, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def _validate_fields(payload: dict, allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise SiriRequestError("Unexpected request field")


def _date(value: str) -> str:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise SiriRequestError("Use an ISO date (YYYY-MM-DD)") from e
    if day < date.today() or day > date.today() + timedelta(days=30):
        raise SiriRequestError("Run date must be within the next 30 days")
    return day.isoformat()


def _run_spec(options: dict) -> tuple[str, WorkoutSpec]:
    if not isinstance(options, dict):
        raise SiriRequestError("options must be an object")
    allowed = {
        "date", "kind", "miles", "repeats", "work_meters", "pace_fast",
        "pace_slow", "recovery_seconds", "recovery_style",
    }
    _validate_fields(options, allowed)
    day = _date(options.get("date"))
    kind = options.get("kind")
    if kind == "easy":
        try:
            miles = float(options["miles"])
        except (KeyError, TypeError, ValueError) as e:
            raise SiriRequestError("Specify an easy-run distance") from e
        if not math.isfinite(miles) or not 1 <= miles <= 30:
            raise SiriRequestError("Easy-run distance must be between 1 and 30 miles")
        # Shared WorkoutKit compiler handles steps and HR alerts.
        spec = WorkoutSpec(
            name=f"Siri Easy {miles:g}mi",
            steps=[StepSpec(kind="run", duration=f"{miles:g}mi", target="hr:2")],
            description="Easy conversational effort; adjust to weather and fatigue.",
            estimated_minutes=int(miles * 9),
        )
    elif kind == "intervals":
        try:
            count = int(options["repeats"])
            meters = int(options["work_meters"])
            recovery = int(options["recovery_seconds"])
        except (KeyError, TypeError, ValueError) as e:
            raise SiriRequestError("Set repeats, work meters, and recovery seconds") from e
        if not 2 <= count <= 20 or not 100 <= meters <= 5000:
            raise SiriRequestError("Use 2-20 repeats of 100-5000 meters")
        if not 30 <= recovery <= 600:
            raise SiriRequestError("Recovery must be 30-600 seconds")
        fast = options.get("pace_fast")
        slow = options.get("pace_slow")
        if not isinstance(fast, str) or not isinstance(slow, str) or not (
            _CLOCK.fullmatch(fast) and _CLOCK.fullmatch(slow)
        ):
            raise SiriRequestError("Paces must use min:sec per mile, e.g. 5:40 and 5:55")
        from .workouts import pace_to_mps

        if pace_to_mps(fast) < pace_to_mps(slow):
            raise SiriRequestError("Fast pace must be faster than slow pace")
        if not 3 * 60 <= int(fast.split(":")[0]) * 60 + int(fast.split(":")[1]) <= 20 * 60:
            raise SiriRequestError("Work pace must be in a realistic training range")
        style = options.get("recovery_style", "jog")
        if style not in ("jog", "standing"):
            raise SiriRequestError("Recovery style must be jog or standing")
        # Standing rest is time-based and carries no HR target. Jog is Z2.
        rest = StepSpec(kind="rest" if style == "standing" else "recovery",
                        duration=f"{recovery}s",
                        label="Standing rest" if style == "standing" else "Easy jog",
                        target=None if style == "standing" else "hr:2")
        spec = WorkoutSpec(
            name=f"Siri {count}x{meters}m {style}",
            description="Warm up and cool down. Confirm this speed session is appropriate.",
            estimated_minutes=60,
            steps=[
                StepSpec(kind="warmup", duration="15:00", target="hr:2"),
                RepeatSpec(repeat=count, steps=[
                    StepSpec(kind="run", duration=f"{meters}m",
                             target=f"pace:{fast}-{slow}"),
                    rest,
                ]),
                StepSpec(kind="cooldown", duration="10:00", target="hr:2"),
            ],
        )
    else:
        raise SiriRequestError("Unsupported run type")
    # Explicitly invoke the WorkoutKit compiler during preview/commit.
    feed_item(day, spec)
    return day, spec


def dispatch(store: FeedStore, payload: dict) -> dict:
    """Restricted, deterministic voice actions. No arbitrary tool invocation."""
    if not isinstance(payload, dict):
        raise SiriRequestError("Request must be a JSON object")
    action = payload.get("action")
    if action == "next_workout":
        _validate_fields(payload, {"action"})
        status = store.status() or {}
        confirmed = {item.get("id"): item.get("rev") for item in status.get("scheduled", [])}
        upcoming = [item for item in store.load()["workouts"] if item["date"] >= date.today().isoformat()]
        upcoming.sort(key=lambda item: (item["date"], item.get("time") or "", item["name"]))
        if not upcoming:
            return {"found": False, "message": "There are no upcoming workouts in your feed."}
        item = upcoming[0]
        is_confirmed = confirmed.get(item["id"]) == item["rev"]
        return {
            "found": True, "date": item["date"], "name": item["name"],
            "sport": item["sport"], "on_watch": is_confirmed,
            "message": (
                f"Next: {item['name']} on {item['date']}. "
                + ("Confirmed from last iPhone sync."
                   if is_confirmed else "Not yet confirmed on Apple Watch.")
            ),
        }
    if action == "texas_races":
        _validate_fields(payload, {"action", "area", "distance"})
        result = texas_race_calendar(area=payload.get("area"), distance=payload.get("distance"))
        shortlist = result["races"][:5]
        return {"count": result["count"], "races": shortlist,
                "message": (f"Found {result['count']} upcoming listed races. "
                            + "; ".join(f"{r['name']}, {r['date'] or 'date to confirm'}"
                                        for r in shortlist)
                            if shortlist else "No matching future races in the curated calendar."),
                "caveat": result["caveat"]}
    if action == "meal_ideas":
        _validate_fields(payload, {"action", "diet"})
        diet = payload.get("diet", "lacto_vegetarian")
        result = meal_plan(diet=diet)
        day = result["days"][0]
        return {"diet": diet, "message": (
            f"Quick plant-based meal ideas: {day['breakfast']}; "
            f"{day['lunch']}; {day['dinner']}. "
            "Open HoodaRun for the seven-day list and grocery shopping."
        )}
    if action in ("preview_run", "publish_run"):
        _validate_fields(payload, {"action", "options", "preview_token"})
        options = payload.get("options")
        day, spec = _run_spec(options)
        record = feed_item(day, spec)
        if action == "preview_run":
            return {
                "preview_token": _token(options), "name": spec.name,
                "date": day, "sport": record["sport"],
                "steps": len(record["workout"]["blocks"]),
                "message": f"Preview: {spec.name} on {day}. Confirmation is required to publish.",
            }
        if not _valid_token(options, payload.get("preview_token")):
            raise SiriRequestError("Preview expired or altered. Preview the run again.")
        result = store.upsert([record])[0]
        return {
            "published": True, "name": result["name"], "date": result["date"],
            "message": (f"Added {result['name']} for {result['date']} to HoodaRun. "
                        "Open the iPhone companion to sync it to Apple Watch."),
        }
    raise SiriRequestError("Unknown Siri action")


class SiriRoutes:
    """Private JSON route mounted before MCP/FeedRoutes, under BearerAuth."""

    def __init__(self, app, store: FeedStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/siri":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self._json(send, 405, {"error": "Use POST"})
            return
        body = bytearray()
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            if len(body) > MAX_VOICE_BODY:
                await self._json(send, 413, {"error": "Request too large"})
                return
            if not msg.get("more_body", False):
                break
        try:
            data = json.loads(body)
            result = dispatch(self.store, data)
        except (ValueError, TypeError, KeyError) as exc:
            await self._json(send, 400, {"error": str(exc)[:250]})
            return
        await self._json(send, 200, result)

    @staticmethod
    async def _json(send, code: int, value: dict):
        response = json.dumps(value, ensure_ascii=True).encode()
        await send({"type": "http.response.start", "status": code, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": response})
