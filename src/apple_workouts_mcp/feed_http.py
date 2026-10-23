"""HTTP delivery of the feed, for when the server runs remotely.

Mounted in front of the MCP app and behind the bearer gate, so HoodaRun uses
the same `Authorization: Bearer <MCP_AUTH_TOKEN>` as Claude does:

    GET  /feed.json   -> workouts.json
    POST /status      <- status.json from the app (max 256 KB)
"""
from __future__ import annotations

import json

from .feed import FeedStore

MAX_STATUS_BYTES = 256 * 1024


class FeedRoutes:
    def __init__(self, app, store: FeedStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path, method = scope.get("path", ""), scope.get("method", "GET")
            if path == "/feed.json" and method == "GET":
                await self._json(send, 200, self.store.load())
                return
            if path == "/status" and method == "POST":
                await self._status(receive, send)
                return
        await self.app(scope, receive, send)

    async def _status(self, receive, send):
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
        self.store.write_status(status)
        await self._json(send, 200, {"ok": True})

    @staticmethod
    async def _json(send, code: int, payload: dict):
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
