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
import os

logger = logging.getLogger("apple_workouts_mcp.http_auth")


class BearerAuthMiddleware:
    """Minimal constant-time bearer check as a pure ASGI wrapper."""

    def __init__(self, app, token: str | None, oauth=None):
        self.app = app
        self._expected = f"Bearer {token}" if token else None
        self._oauth = oauth   # OAuthProvider: also accept its access tokens
        self._challenge = (
            f'Bearer resource_metadata="{oauth.base}/.well-known/oauth-protected-resource"'
            .encode() if oauth else b"Bearer")

    def _allowed(self, provided: str) -> bool:
        if self._expected and provided and hmac.compare_digest(provided, self._expected):
            return True
        if self._oauth and provided[:7].lower() == "bearer ":
            return self._oauth.verify(provided[7:].strip())
        return False

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
        if not self._allowed(provided):
            await self._reject(send)
            return
        await self.app(scope, receive, send)

    async def _reject(self, send):
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"www-authenticate", self._challenge)],
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

    oauth = None
    if token and store is not None:
        from .oauth import OAuthProvider

        oauth = OAuthProvider.from_env(store.dir, token)

    if token:
        app = BearerAuthMiddleware(app, token, oauth=oauth)
        logger.info("HTTP transport secured with bearer token%s.",
                    " + OAuth (Claude/ChatGPT connectors)" if oauth else "")
        if oauth:
            from .oauth import OAuthRoutes

            app = OAuthRoutes(app, oauth)   # public: discovery, register, sign-in, token
    if store is not None:
        from .feed_http import CalendarSubscriptionRoutes

        # Public path, secret URL, read-only: Apple Calendar can't send headers.
        app = CalendarSubscriptionRoutes(app, store, os.environ.get("CALENDAR_TOKEN"))
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set — the HTTP server is UNAUTHENTICATED and can "
            "write to your workout feed. Set MCP_AUTH_TOKEN before exposing it."
        )
    uvicorn.run(app, host=host, port=port)
