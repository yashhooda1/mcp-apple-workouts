"""OAuth 2.1 for Claude/ChatGPT connectors: discovery, DCR, PKCE, rotation, gate."""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apple_workouts_mcp.http_auth import BearerAuthMiddleware
from apple_workouts_mcp.oauth import OAuthProvider, OAuthRoutes

BASE = "https://run.example.com"
CB = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()) \
    .rstrip(b"=").decode()


@pytest.fixture
def setup(tmp_path):
    provider = OAuthProvider(BASE, "owner-pw", tmp_path / "oauth.json")

    async def mcp(_r):
        return PlainTextResponse("mcp ok")
    app = OAuthRoutes(BearerAuthMiddleware(Starlette(routes=[Route("/mcp", mcp)]),
                                           "static-tok", oauth=provider), provider)
    return provider, TestClient(app, follow_redirects=False)


def _register(c, uri=CB):
    return c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [uri]})


def _authorize(c, cid, pw="owner-pw", **extra):
    form = {"client_id": cid, "redirect_uri": CB, "response_type": "code", "state": "xyz",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "password": pw,
            **extra}
    return c.post("/oauth/authorize", data=form)


def _code(c, cid):
    r = _authorize(c, cid)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    return q["code"][0]


def test_discovery_and_401_challenge(setup):
    _, c = setup
    pr = c.get("/.well-known/oauth-protected-resource").json()
    assert pr["resource"] == f"{BASE}/mcp" and pr["authorization_servers"] == [BASE]
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert md["code_challenge_methods_supported"] == ["S256"]
    r = c.get("/mcp")
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]


def test_full_flow_and_refresh_rotation(setup):
    _, c = setup
    cid = _register(c).json()["client_id"]
    page = c.get("/oauth/authorize", params={
        "client_id": cid, "redirect_uri": CB, "response_type": "code",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "xyz"})
    assert page.status_code == 200 and "Connect Claude" in page.text
    assert page.headers["x-frame-options"] == "DENY"

    code = _code(c, cid)
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                       "client_id": cid, "redirect_uri": CB,
                                       "code_verifier": VERIFIER}).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    assert c.get("/mcp", headers=auth).text == "mcp ok"
    # Code is single-use.
    again = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                         "client_id": cid, "redirect_uri": CB,
                                         "code_verifier": VERIFIER})
    assert again.status_code == 400

    new = c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
                                       "refresh_token": tok["refresh_token"]}).json()
    assert new["access_token"] != tok["access_token"]
    reused = c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": cid,
                                          "refresh_token": tok["refresh_token"]})
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    # Static token still works for HoodaRun / Claude Desktop.
    assert c.get("/mcp", headers={"Authorization": "Bearer static-tok"}).status_code == 200


def test_pkce_mismatch_rejected(setup):
    _, c = setup
    cid = _register(c).json()["client_id"]
    code = _code(c, cid)
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                     "client_id": cid, "redirect_uri": CB,
                                     "code_verifier": "w" * 64})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_redirect_allow_list(setup):
    _, c = setup
    assert _register(c, "https://evil.example/cb").status_code == 400
    assert _register(c, "http://claude.ai/cb").status_code == 400
    assert _register(c, "https://chatgpt.com/connector_platform_oauth_redirect").status_code == 201
    assert _register(c, "http://localhost:6274/oauth/callback").status_code == 201


def test_unregistered_redirect_and_missing_pkce(setup):
    _, c = setup
    cid = _register(c).json()["client_id"]
    r = c.get("/oauth/authorize", params={"client_id": cid, "redirect_uri": "https://claude.ai/x",
                                          "response_type": "code", "code_challenge": CHALLENGE,
                                          "code_challenge_method": "S256"})
    assert r.status_code == 400 and "<input" not in r.text
    r = c.get("/oauth/authorize", params={"client_id": cid, "redirect_uri": CB,
                                          "response_type": "code"})
    assert r.status_code == 400


def test_wrong_password_then_lockout(setup):
    _, c = setup
    cid = _register(c).json()["client_id"]
    for _ in range(5):
        r = _authorize(c, cid, pw="nope")
        assert r.status_code == 401 and "Wrong password" in r.text
    assert _authorize(c, cid).status_code == 429


def test_confidential_client_needs_secret(setup):
    _, c = setup
    reg = c.post("/oauth/register", json={"redirect_uris": [CB],
                                          "token_endpoint_auth_method": "client_secret_post"}
                 ).json()
    code = _code(c, reg["client_id"])
    bad = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                       "client_id": reg["client_id"], "redirect_uri": CB,
                                       "code_verifier": VERIFIER})
    assert bad.status_code == 401
    code = _code(c, reg["client_id"])
    basic = base64.b64encode(f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
    ok = c.post("/oauth/token", headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                      "code_verifier": VERIFIER})
    assert ok.status_code == 200


def test_tokens_stored_hashed(setup):
    provider, c = setup
    cid = _register(c).json()["client_id"]
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code",
                                       "code": _code(c, cid), "client_id": cid,
                                       "redirect_uri": CB, "code_verifier": VERIFIER}).json()
    raw = provider.path.read_text()
    assert tok["access_token"] not in raw and tok["refresh_token"] not in raw


def test_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert OAuthProvider.from_env(tmp_path, "t") is None
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_HOSTS", "claude.ai")
    p = OAuthProvider.from_env(tmp_path, "t")
    assert p.allowed_hosts == ("claude.ai",)
