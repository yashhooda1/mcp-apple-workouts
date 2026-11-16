"""HTTP delivery of the feed, for when the server runs remotely.

Mounted in front of the MCP app and behind the bearer gate, so HoodaRun uses
the same `Authorization: Bearer <MCP_AUTH_TOKEN>` as Claude does:

    GET  /feed.json   -> workouts.json
    POST /status      <- status.json from the app (max 256 KB)
    POST /completed   <- finished workouts from Health (see completed.py)
    GET  /calendar.ics -> the feed as an iCalendar (see ical.py)

`CalendarSubscriptionRoutes` sits OUTSIDE the gate and serves
`GET /calendar/<CALENDAR_TOKEN>.ics` for Apple Calendar subscriptions. It is
read-only and uses its own secret, so a leaked calendar URL never grants
write access to /mcp or the feed.
"""
from __future__ import annotations

import hmac
import json
from datetime import date, timedelta

from . import completed
from .feed import FeedStore
from .http_auth import scoped_store
from .ical import build_calendar

MAX_STATUS_BYTES = 256 * 1024
CALENDAR_HISTORY_DAYS = 60


def calendar_body(store: FeedStore) -> bytes:
    since = (date.today() - timedelta(days=CALENDAR_HISTORY_DAYS)).isoformat()
    return build_calendar(store.load()["workouts"], since=since).encode()


async def send_calendar(send, body: bytes) -> None:
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/calendar; charset=utf-8"),
                            (b"content-disposition", b'inline; filename="hoodarun.ics"'),
                            (b"cache-control", b"no-cache")]})
    await send({"type": "http.response.body", "body": body})


class CalendarSubscriptionRoutes:
    """Public-path, secret-URL calendar feed. 404 unless CALENDAR_TOKEN is set."""

    def __init__(self, app, store: FeedStore, token: str | None, accounts=None):
        self.app = app
        self.store = store
        self._expected = f"/calendar/{token}.ics".encode() if token else None
        self._accounts = accounts   # AccountStore: members have their own URL

    def _store_for(self, path: str) -> FeedStore | None:
        """Whose calendar this secret URL opens, if anyone's."""
        if self._expected and hmac.compare_digest(path.encode(), self._expected):
            return self.store
        if self._accounts and path.endswith(".ics"):
            user = self._accounts.by_calendar_token(path[len("/calendar/"):-len(".ics")])
            if user:
                return self._accounts.store_for(user)
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/calendar/"):
            store = self._store_for(scope["path"]) \
                if scope.get("method") in ("GET", "HEAD") else None
            if store is not None:
                await send_calendar(send, calendar_body(store))
                return
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"not found"})
            return
        await self.app(scope, receive, send)


class FeedRoutes:
    def __init__(self, app, store: FeedStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path, method = scope.get("path", ""), scope.get("method", "GET")
            store = scoped_store(scope, self.store)   # the signed-in person's feed
            if path == "/feed.json" and method == "GET":
                await self._json(send, 200, store.load())
                return
            if path == "/calendar.ics" and method == "GET":
                await send_calendar(send, calendar_body(store))
                return
            if path == "/status" and method == "POST":
                await self._status(store, receive, send)
                return
            if path == "/completed" and method == "POST":
                await self._completed(store, receive, send)
                return
        await self.app(scope, receive, send)

    async def _read_json(self, receive, send, what: str):
        """The request body as a dict, or None after answering with the error."""
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_STATUS_BYTES:
                await self._json(send, 413, {"error": f"{what} too large"})
                return None
        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            await self._json(send, 400, {"error": f"{what} must be a JSON object"})
            return None
        return data

    async def _completed(self, store, receive, send):
        """Finished workouts from Health, reported by HoodaRun (completed.py)."""
        data = await self._read_json(receive, send, "completed workouts")
        if data is None:
            return
        rows = data.get("workouts")
        if not isinstance(rows, list):
            await self._json(send, 400, {"error": "expected {\"workouts\": [...]}"})
            return
        await self._json(send, 200, completed.record(store, rows))

    async def _status(self, store, receive, send):
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_STATUS_BYTES:
                await self._json(send, 413, {"error": "status too large"})
                return
        try:
            status = json.loads(body or b"{}")
            if not isinstance(status, dict):
                raise ValueError
        except ValueError:
            await self._json(send, 400, {"error": "status must be a JSON object"})
            return
        store.write_status(status)
        await self._json(send, 200, {"ok": True})

    @staticmethod
    async def _json(send, code: int, payload: dict):
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
