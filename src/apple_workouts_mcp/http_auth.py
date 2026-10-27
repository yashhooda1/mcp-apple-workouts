"""Bearer-token gate for the HTTP transports.

Wraps the FastMCP ASGI app so every HTTP request must present
`Authorization: Bearer <MCP_AUTH_TOKEN>`. Without it the server returns 401.
Lifespan/websocket scopes pass through untouched so the MCP session manager
still starts normally.

Generate a token with:  openssl rand -hex 32
"""
from __future__ import annotations

import hmac
import logging

logger = logging.getLogger("apple_workouts_mcp.http_auth")


class BearerAuthMiddleware:
    """Minimal constant-time bearer check as a pure ASGI wrapper."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Static app shell contains no private data. Every API, feed and MCP
        # route still requires the same bearer token as before.
        if scope.get("method") == "GET" and scope.get("path") in (
            "/", "/app", "/app/", "/app/app.css", "/app/app.js"
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if not (provided and hmac.compare_digest(provided, self._expected)):
            await self._reject(send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send):
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer")],
        })
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


def serve_http(mcp, transport: str, host: str, port: int, token: str | None,
               store=None) -> None:
    """Build the FastMCP HTTP app, add the feed routes, gate it, serve with uvicorn."""
    import uvicorn

    app = (mcp.streamable_http_app() if transport == "streamable-http"
           else mcp.sse_app())
    if store is not None:
        from .feed_http import FeedRoutes

        app = FeedRoutes(app, store)   # inside the gate: same bearer token
        from .siri import SiriRoutes

        app = SiriRoutes(app, store)   # private /api/siri voice bridge
        from .web_app import WebRoutes

        app = WebRoutes(app, store)  # app shell public; API inside the gate

    # Refuse unauthenticated Internet-facing transports. Loopback can be used
    # by trusted local clients (for example a secure MCP tunnel).
    if not token and host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(
            "Refusing to expose writable MCP/WorkoutKit feed without MCP_AUTH_TOKEN. "
            "Set MCP_AUTH_TOKEN or bind HOST=127.0.0.1 for local development."
        )

    if token:
        app = BearerAuthMiddleware(app, token)
        logger.info("HTTP transport secured with bearer token.")
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set — the HTTP server is UNAUTHENTICATED and can "
            "write to your workout feed. Set MCP_AUTH_TOKEN before exposing it."
        )
    uvicorn.run(app, host=host, port=port)
