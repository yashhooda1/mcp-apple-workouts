"""Bearer-token gate for the HTTP transports.

Wraps the FastMCP ASGI app so every HTTP request must present a bearer token.
Without one the server returns 401. Lifespan/websocket scopes pass through
untouched so the MCP session manager still starts normally.

Three kinds of token are accepted, and each one names a person:

  * `MCP_AUTH_TOKEN`               -> the owner (HoodaRun, Claude Desktop, API)
  * an OAuth access token          -> whoever signed in to that connector
  * a member's HoodaRun app token  -> that member

The gate records who it is in the ASGI scope (`hooda.user`) together with
that person's feed (`hooda.store`). Everything behind the gate — the MCP
tools, /feed.json, /status, /api/siri, /api/web — reads the feed from there,
so no route can reach another person's workouts by construction.

Generate a token with:  openssl rand -hex 32
"""
from __future__ import annotations

import hmac
import logging
import os

from .accounts import OWNER

logger = logging.getLogger("apple_workouts_mcp.http_auth")

# ASGI scope keys set by the gate for everything behind it.
SCOPE_USER = "hooda.user"
SCOPE_STORE = "hooda.store"
SCOPE_IDENTITY = "hooda.identity"


def scoped_store(scope, default):
    """The signed-in person's feed, or `default` on a single-owner server."""
    return scope.get(SCOPE_STORE) or default


class BearerAuthMiddleware:
    """Minimal constant-time bearer check as a pure ASGI wrapper."""

    def __init__(self, app, token: str | None, oauth=None, accounts=None):
        self.app = app
        self._expected = f"Bearer {token}" if token else None
        self._oauth = oauth   # OAuthProvider: also accept its access tokens
        self._accounts = accounts or (oauth.accounts if oauth else None)
        self._challenge = (
            f'Bearer resource_metadata="{oauth.base}/.well-known/oauth-protected-resource"'
            .encode() if oauth else b"Bearer")

    def _who(self, provided: str) -> str | None:
        """The account id this Authorization header belongs to, or None."""
        if self._expected and provided and hmac.compare_digest(provided, self._expected):
            return OWNER
        if provided[:7].lower() != "bearer ":
            return None
        token = provided[7:].strip()
        if self._oauth:
            user = self._oauth.verify(token)
            if user:
                return user
        if self._accounts:
            return self._accounts.by_app_token(token)
        return None

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
        user = self._who(provided)
        if user is None:
            await self._reject(send)
            return
        if self._accounts:
            scope = dict(scope)
            scope[SCOPE_USER] = user
            scope[SCOPE_STORE] = self._accounts.store_for(user)
            scope[SCOPE_IDENTITY] = self._accounts.identity(user)
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
    """Build the gated app (see build_app) and serve it with uvicorn."""
    import uvicorn

    # Said out loud because a host that routes traffic to a different port than
    # this one shows a healthy-looking deploy and answers every request 502.
    logger.info("Listening on %s:%s (PORT=%s). The public domain's target port must "
                "match.", host, port, os.environ.get("PORT", "unset"))
    uvicorn.run(HealthRoute(build_app(mcp, transport, host, token, store)),
                host=host, port=port)


class HealthRoute:
    """GET /healthz -> 200, no auth, no data. Lets the host check that the
    process is really answering on the port it routes to before it switches
    traffic to a new deploy (railway.toml: healthcheckPath)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b'{"ok": true}'})
            return
        await self.app(scope, receive, send)


def build_app(mcp, transport: str, host: str, token: str | None, store=None):
    """The FastMCP HTTP app with the feed routes, the gate, and the public
    OAuth / account / calendar routes layered around it."""
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

    oauth = accounts = None
    if token and store is not None:
        from .accounts import AccountStore
        from .oauth import OAuthProvider

        accounts = AccountStore.from_env(store.dir, token)
        oauth = OAuthProvider.from_env(store.dir, token, accounts=accounts)

    if token:
        app = BearerAuthMiddleware(app, token, oauth=oauth, accounts=accounts)
        logger.info("HTTP transport secured with bearer token%s.",
                    " + OAuth (Claude/ChatGPT connectors)" if oauth else "")
        if oauth:
            from .account_http import AccountRoutes
            from .oauth import OAuthRoutes

            try:
                from .apple_signin import AppleSignIn

                oauth.apple = AppleSignIn.from_env(oauth.base)
            except ImportError:   # PyJWT[crypto] not installed
                logger.warning("Sign in with Apple is configured but PyJWT[crypto] "
                               "is missing; leaving it off.")
            logger.info("Accounts: sign-up %s, Sign in with Apple %s.",
                        "open with invite code" if accounts.signup_open else "closed",
                        "on" if oauth.apple else "off")
            app = AccountRoutes(app, oauth, oauth.apple)   # public: sign-up, account, Apple
            app = OAuthRoutes(app, oauth)   # public: discovery, register, sign-in, token
    if store is not None:
        from .feed_http import CalendarSubscriptionRoutes

        # Public path, secret URL, read-only: Apple Calendar can't send headers.
        app = CalendarSubscriptionRoutes(app, store, os.environ.get("CALENDAR_TOKEN"),
                                         accounts=accounts)
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set — the HTTP server is UNAUTHENTICATED and can "
            "write to your workout feed. Set MCP_AUTH_TOKEN before exposing it."
        )
    return app
