"""Sign in with Apple, as a second way into an account.

Off unless all four variables are set (they come from an Apple Developer
Program membership — Certificates, Identifiers & Profiles):

    APPLE_CLIENT_ID     the Services ID, e.g. ai.yashhooda.hoodarun.web
    APPLE_TEAM_ID       10-character team id
    APPLE_KEY_ID        id of a "Sign in with Apple" key
    APPLE_PRIVATE_KEY   that key's .p8 contents (newlines may be written as \\n)

In the Services ID's configuration, add the server's host as a domain and
`<PUBLIC_BASE_URL>/oauth/apple/callback` as a return URL.

What it proves is *who someone is* — a stable Apple account id, and their
email the first time. It does not give this server any access to their iCloud
or their calendar: Apple offers no such API. Their calendar still arrives by
subscription URL or through HoodaRun, exactly as for a password account.

Flow (web, `response_mode=form_post`):

    GET  /oauth/apple/start  -> 302 to appleid.apple.com with state + nonce
    POST /oauth/apple/callback (from Apple) -> code exchanged at Apple's token
         endpoint with an ES256 client secret; the returned id_token is
         verified against Apple's published keys (issuer, audience, expiry,
         nonce) before anything is trusted.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from .accounts import AccountError

AUTH_URL = "https://appleid.apple.com/auth/authorize"
TOKEN_URL = "https://appleid.apple.com/auth/token"
KEYS_URL = "https://appleid.apple.com/auth/keys"
ISSUER = "https://appleid.apple.com"
STATE_TTL = 600
KEYS_TTL = 3600
MAX_PENDING = 500


@dataclass(frozen=True)
class AppleUser:
    sub: str
    email: Optional[str]
    name: str


class AppleSignIn:
    def __init__(self, client_id: str, team_id: str, key_id: str, private_key: str,
                 redirect_uri: str, http=None):
        import jwt  # PyJWT[crypto]; imported here so the server runs without it

        self._jwt = jwt
        self.client_id, self.team_id, self.key_id = client_id, team_id, key_id
        self._private_key = private_key.replace("\\n", "\n")
        self.redirect_uri = redirect_uri
        self._http = http
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}
        self._keys: tuple[float, dict] = (0.0, {})

    @classmethod
    def from_env(cls, base_url: str) -> Optional["AppleSignIn"]:
        env = os.environ.get
        values = [env("APPLE_CLIENT_ID"), env("APPLE_TEAM_ID"), env("APPLE_KEY_ID"),
                  env("APPLE_PRIVATE_KEY")]
        if not all(values):
            return None
        return cls(*values, redirect_uri=f"{base_url.rstrip('/')}/oauth/apple/callback")

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=10)
        return self._http

    # ----------------------------------------------------------------- start
    def begin(self, payload: dict) -> str:
        """Remember `payload` (what to do after sign-in) and return Apple's URL."""
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._states = {k: v for k, v in self._states.items() if v["exp"] > now}
            if len(self._states) >= MAX_PENDING:
                raise AccountError("Too many sign-ins in progress. Try again shortly.", 429)
            self._states[state] = {"payload": payload, "nonce": nonce, "exp": now + STATE_TTL}
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "response_mode": "form_post",
            "scope": "name email", "state": state, "nonce": nonce})

    # -------------------------------------------------------------- callback
    def complete(self, form: dict) -> tuple[dict, AppleUser]:
        """Validate Apple's POST. Returns (the payload from begin(), who it is)."""
        with self._lock:
            pending = self._states.pop(form.get("state") or "", None)   # single use
        if not pending or pending["exp"] < time.time():
            raise AccountError("That Apple sign-in expired. Start again.", 400)
        if form.get("error") or not form.get("code"):
            raise AccountError("Apple sign-in was cancelled.", 400)
        claims = self._verify(self._exchange(form["code"]), pending["nonce"])
        name = ""
        try:   # Apple sends the name once, on the very first authorization
            n = (json.loads(form.get("user") or "{}").get("name") or {})
            name = " ".join(x for x in (n.get("firstName"), n.get("lastName")) if x)
        except (ValueError, AttributeError):
            pass
        verified = str(claims.get("email_verified", "")).lower() == "true"
        return pending["payload"], AppleUser(
            sub=claims["sub"], email=claims.get("email") if verified else None, name=name)

    def _client_secret(self) -> str:
        now = int(time.time())
        return self._jwt.encode(
            {"iss": self.team_id, "iat": now, "exp": now + 300, "aud": ISSUER,
             "sub": self.client_id},
            self._private_key, algorithm="ES256", headers={"kid": self.key_id})

    def _exchange(self, code: str) -> str:
        r = self._client().post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self._client_secret(),
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri})
        if r.status_code != 200 or "id_token" not in r.json():
            raise AccountError("Apple didn't accept that sign-in. Start again.", 502)
        return r.json()["id_token"]

    def _key_for(self, kid: str):
        fetched, keys = self._keys
        if kid not in keys or time.time() - fetched > KEYS_TTL:
            r = self._client().get(KEYS_URL)
            if r.status_code != 200:
                raise AccountError("Couldn't reach Apple to verify the sign-in.", 502)
            keys = {k["kid"]: k for k in r.json().get("keys", [])}
            self._keys = (time.time(), keys)
        if kid not in keys:
            raise AccountError("Apple sign-in could not be verified.", 400)
        return self._jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(keys[kid]))

    def _verify(self, id_token: str, nonce: str) -> dict:
        jwt = self._jwt
        try:
            kid = jwt.get_unverified_header(id_token)["kid"]
            claims = jwt.decode(id_token, self._key_for(kid), algorithms=["RS256"],
                                audience=self.client_id, issuer=ISSUER,
                                options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except (jwt.PyJWTError, KeyError) as exc:
            raise AccountError("Apple sign-in could not be verified.", 400) from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AccountError("Apple sign-in could not be verified.", 400)
        return claims
