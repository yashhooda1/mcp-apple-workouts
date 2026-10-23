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
