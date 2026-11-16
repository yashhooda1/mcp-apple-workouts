"""Sign in with Apple against a stand-in for Apple.

Apple itself can't be reached from a test, so `FakeApple` plays its token and
keys endpoints: it checks the client secret the way Apple would and signs
id_tokens with its own RSA key. That exercises everything on this side — the
state/nonce handling, the ES256 client secret, id_token verification, and the
account flow — but not Apple's real behaviour or a real Services ID.
"""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from starlette.testclient import TestClient

from apple_workouts_mcp.account_http import AccountRoutes
from apple_workouts_mcp.accounts import AccountError, AccountStore
from apple_workouts_mcp.apple_signin import ISSUER, KEYS_URL, TOKEN_URL, AppleSignIn
from apple_workouts_mcp.oauth import OAuthProvider, OAuthRoutes

BASE = "https://run.example.com"
CB = "https://claude.ai/api/mcp/auth_callback"
CLIENT_ID = "ai.example.hoodarun.web"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()) \
    .rstrip(b"=").decode()
INVITE = "invite-xyz"


def _pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


class _Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeApple:
    """Apple's /auth/token and /auth/keys."""

    def __init__(self, team_key_public):
        self.team_key_public = team_key_public
        self.signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "apple-key-1"
        self.claims = {}          # what the next id_token should say
        self.sign_with = None     # override to forge a token with another key

    def post(self, url, data):
        assert url == TOKEN_URL
        secret = jwt.decode(data["client_secret"], self.team_key_public, algorithms=["ES256"],
                            audience=ISSUER)
        assert secret["sub"] == CLIENT_ID == data["client_id"] and secret["iss"] == "TEAM123456"
        assert data["grant_type"] == "authorization_code" and data["redirect_uri"]
        if data["code"] != "good-code":
            return _Response(400, {"error": "invalid_grant"})
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                  "sub": "001234.apple-user", "email": "runner@example.com",
                  "email_verified": "true", **self.claims}
        token = jwt.encode(claims, self.sign_with or self.signing, algorithm="RS256",
                           headers={"kid": self.kid})
        return _Response(200, {"id_token": token})

    def get(self, url):
        assert url == KEYS_URL
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.signing.public_key()))
        return _Response(200, {"keys": [{**jwk, "kid": self.kid, "alg": "RS256"}]})


@pytest.fixture
def apple():
    team_key = ec.generate_private_key(ec.SECP256R1())
    fake = FakeApple(team_key.public_key())
    signin = AppleSignIn(CLIENT_ID, "TEAM123456", "KEYID12345",
                         _pem(team_key).replace("\n", "\\n"),   # as pasted into an env var
                         f"{BASE}/oauth/apple/callback", http=fake)
    return signin, fake


def _begin(signin, fake, payload=None):
    q = parse_qs(urlparse(signin.begin(payload or {"carry": {}})).query)
    fake.claims["nonce"] = q["nonce"][0]
    return q["state"][0], q


def test_start_url_and_successful_sign_in(apple):
    signin, fake = apple
    state, q = _begin(signin, fake, {"carry": {"x": "1"}})
    assert q["client_id"] == [CLIENT_ID] and q["response_mode"] == ["form_post"]
    assert q["redirect_uri"] == [f"{BASE}/oauth/apple/callback"] and q["scope"] == ["name email"]
    user_json = json.dumps({"name": {"firstName": "Ada", "lastName": "Runner"}})
    payload, who = signin.complete({"state": state, "code": "good-code", "user": user_json})
    assert payload == {"carry": {"x": "1"}}
    assert (who.sub, who.email, who.name) == ("001234.apple-user", "runner@example.com",
                                              "Ada Runner")
    with pytest.raises(AccountError, match="expired"):      # state is single-use
        signin.complete({"state": state, "code": "good-code"})


@pytest.mark.parametrize("claims", [
    {"nonce": "someone-elses"}, {"aud": "another.app"}, {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 10},
])
def test_id_token_with_wrong_claims_is_rejected(apple, claims):
    signin, fake = apple
    state, _ = _begin(signin, fake)
    fake.claims.update(claims)
    with pytest.raises(AccountError, match="could not be verified"):
        signin.complete({"state": state, "code": "good-code"})


def test_forged_signature_cancel_and_bad_code_are_rejected(apple):
    signin, fake = apple
    state, _ = _begin(signin, fake)
    fake.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AccountError, match="could not be verified"):
        signin.complete({"state": state, "code": "good-code"})
    fake.sign_with = None
    state, _ = _begin(signin, fake)
    with pytest.raises(AccountError, match="cancelled"):
        signin.complete({"state": state, "error": "user_cancelled_authorize"})
    state, _ = _begin(signin, fake)
    with pytest.raises(AccountError, match="didn't accept"):
        signin.complete({"state": state, "code": "stolen-code"})
    with pytest.raises(AccountError, match="expired"):
        signin.complete({"state": "never-issued", "code": "good-code"})


def test_unverified_email_is_not_trusted(apple):
    signin, fake = apple
    state, _ = _begin(signin, fake)
    fake.claims["email_verified"] = "false"
    assert signin.complete({"state": state, "code": "good-code"})[1].email is None


def test_from_env_needs_all_four(monkeypatch):
    for k in ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert AppleSignIn.from_env(BASE) is None
    monkeypatch.setenv("APPLE_CLIENT_ID", CLIENT_ID)
    assert AppleSignIn.from_env(BASE) is None


# ------------------------------------------------------------------ over HTTP
@pytest.fixture
def site(tmp_path, apple):
    signin, fake = apple
    accounts = AccountStore(tmp_path, "owner-pw", invite_code=INVITE)
    provider = OAuthProvider(BASE, "owner-pw", tmp_path / "oauth.json", accounts=accounts)
    provider.apple = signin

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    app = OAuthRoutes(AccountRoutes(inner, provider, signin), provider)
    return TestClient(app, base_url=BASE, follow_redirects=False), provider, fake


def _connector_request(c):
    cid = c.post("/oauth/register", json={"redirect_uris": [CB]}).json()["client_id"]
    return {"client_id": cid, "redirect_uri": CB, "response_type": "code", "state": "xyz",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"}


def _to_apple_and_back(c, fake, params=None):
    r = c.get("/oauth/apple/start", params=params or {})
    assert r.status_code == 302 and r.headers["location"].startswith(
        "https://appleid.apple.com/auth/authorize?")
    q = parse_qs(urlparse(r.headers["location"]).query)
    fake.claims["nonce"] = q["nonce"][0]
    return c.post("/oauth/apple/callback", data={"state": q["state"][0], "code": "good-code"})


def test_first_apple_sign_in_needs_the_invite_then_connects(site):
    c, provider, fake = site
    q = _connector_request(c)
    assert "Sign in with Apple" in c.get("/oauth/authorize", params=q).text

    page = _to_apple_and_back(c, fake, q)
    assert page.status_code == 200 and "invite code" in page.text
    pending = page.text.split('name="pending" value="')[1].split('"')[0]
    wrong = c.post("/oauth/apple/finish", data={"pending": pending, "invite": "nope"})
    assert wrong.status_code == 403 and provider.accounts.members() == []

    done = c.post("/oauth/apple/finish", data={"pending": pending, "invite": INVITE})
    assert done.status_code == 302 and done.headers["location"].startswith(CB)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    token = c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": q["client_id"],
        "redirect_uri": CB, "code_verifier": VERIFIER}).json()["access_token"]
    member = provider.accounts.members()[0]
    assert member["email"] == "runner@example.com" and member["apple"] and not member["password"]
    assert provider.verify(token) == member["id"]
    assert c.post("/oauth/apple/finish", data={"pending": pending, "invite": INVITE}) \
        .status_code == 400                                 # the pending step is single-use

    # the next time, the same Apple account goes straight through — no invite
    again = _to_apple_and_back(c, fake, _connector_request(c))
    assert again.status_code == 302 and again.headers["location"].startswith(CB)
    assert len(provider.accounts.members()) == 1


def test_apple_sign_in_without_a_connector_lands_on_the_account_page(site):
    c, provider, fake = site
    page = _to_apple_and_back(c, fake)
    pending = page.text.split('name="pending" value="')[1].split('"')[0]
    done = c.post("/oauth/apple/finish", data={"pending": pending, "invite": INVITE})
    assert done.status_code == 200 and "webcal://run.example.com/calendar/" in done.text


def test_apple_does_not_take_over_an_existing_password_account(site):
    c, provider, fake = site
    provider.accounts.create("runner@example.com", "correct-horse-1", invite=INVITE)
    page = _to_apple_and_back(c, fake)
    pending = page.text.split('name="pending" value="')[1].split('"')[0]
    done = c.post("/oauth/apple/finish", data={"pending": pending, "invite": INVITE})
    assert done.status_code == 409 and "already has an account" in done.text
    assert not provider.accounts.members()[0]["apple"]


def test_apple_callback_with_a_forged_state_is_refused(site):
    c, _, _ = site
    r = c.post("/oauth/apple/callback", data={"state": "made-up", "code": "good-code"})
    assert r.status_code == 400 and "expired" in r.text
