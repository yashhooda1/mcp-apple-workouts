"""The bearer gate returns 401 without a token and passes auth'd requests through."""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apple_workouts_mcp.http_auth import BearerAuthMiddleware

TOKEN = "s3cret-test-token"


def _inner():
    async def ok(_request):
        return PlainTextResponse("ok")
    return Starlette(routes=[Route("/mcp", ok)])


def test_rejects_without_token():
    client = TestClient(BearerAuthMiddleware(_inner(), TOKEN))
    r = client.get("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_rejects_wrong_token():
    client = TestClient(BearerAuthMiddleware(_inner(), TOKEN))
    r = client.get("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_allows_correct_token():
    client = TestClient(BearerAuthMiddleware(_inner(), TOKEN))
    r = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.text == "ok"


def test_hosted_server_accepts_its_public_hostname(monkeypatch):
    """A deployed server must not answer its own hostname with 421.

    FastMCP's default only allows Host: localhost; serve_http() runs behind a
    proxy on e.g. <app>.up.railway.app, so the module turns that check off.
    """
    import importlib

    from apple_workouts_mcp import server

    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.setenv("HOST", "0.0.0.0")
    try:
        hosted = importlib.reload(server)
        security = hosted.mcp.settings.transport_security
        assert security is not None and not security.enable_dns_rebinding_protection

        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        with TestClient(hosted.mcp.streamable_http_app(),
                        base_url="https://apple-mcp.up.railway.app") as client:
            r = client.post("/mcp", json=init,
                            headers={"accept": "application/json, text/event-stream",
                                     "origin": "https://claude.ai"})
        assert r.status_code == 200, r.text
    finally:
        monkeypatch.undo()
        importlib.reload(server)   # back to the default for the other tests


def test_local_stdio_keeps_the_sdk_default(monkeypatch):
    from apple_workouts_mcp import server

    monkeypatch.delenv("TRANSPORT", raising=False)
    assert server._transport_security() is None


def test_healthz_is_public_and_says_nothing():
    from apple_workouts_mcp.http_auth import HealthRoute

    client = TestClient(HealthRoute(BearerAuthMiddleware(_inner(), TOKEN)))
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/mcp").status_code == 401          # everything else still gated
