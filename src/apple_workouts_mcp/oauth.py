"""Self-hosted OAuth 2.1 for the remote MCP endpoint — Claude and ChatGPT connectors.

Claude's custom connectors and ChatGPT's connectors/apps sign in to a remote
MCP server with OAuth rather than a pasted bearer token. This is a small,
single-owner authorization server built into the MCP server itself — no
third-party identity provider:

    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728
    GET  /.well-known/oauth-authorization-server       RFC 8414
    POST /oauth/register                               RFC 7591 dynamic client registration
    GET  /oauth/authorize                              owner sign-in page
    POST /oauth/authorize                              -> 302 back with ?code=
    POST /oauth/token                                  authorization_code (PKCE S256) | refresh_token

"Sign in" means typing the owner secret (`OAUTH_OWNER_PASSWORD`, default:
`MCP_AUTH_TOKEN`). Only redirect URIs on allow-listed hosts can register
(`OAUTH_ALLOWED_REDIRECT_HOSTS`, default Claude + ChatGPT + localhost).
PKCE S256 is mandatory, codes are single-use, refresh tokens rotate, and only
SHA-256 hashes of tokens are stored (in `oauth.json` next to the feed — keep
`WORKOUT_FEED_DIR` on a persistent volume so connectors stay signed in).

Enabled when `PUBLIC_BASE_URL` is set (e.g. https://hoodarun.up.railway.app).
The static `MCP_AUTH_TOKEN` keeps working for HoodaRun and Claude Desktop.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger("apple_workouts_mcp.oauth")

DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "chatgpt.com", "chat.openai.com",
                          "localhost", "127.0.0.1")
SCOPE = "workouts"
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
MAX_BODY = 64 * 1024
MAX_CLIENTS = 50


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


class OAuthProvider:
    def __init__(self, base_url: str, owner_secret: str, state_path: Path,
                 allowed_hosts: Optional[tuple[str, ...]] = None):
        if not owner_secret:
            raise ValueError("OAuth needs OAUTH_OWNER_PASSWORD or MCP_AUTH_TOKEN")
        self.base = base_url.rstrip("/")
        self._secret = owner_secret
        self.path = Path(state_path)
        self.allowed_hosts = tuple(h.lower() for h in (allowed_hosts or DEFAULT_REDIRECT_HOSTS))
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, store_dir: Path, token: Optional[str]) -> Optional["OAuthProvider"]:
        base = os.environ.get("PUBLIC_BASE_URL")
        if not base:
            return None
        hosts = os.environ.get("OAUTH_ALLOWED_REDIRECT_HOSTS")
        return cls(base, os.environ.get("OAUTH_OWNER_PASSWORD") or token or "",
                   Path(store_dir) / "oauth.json",
                   tuple(h.strip() for h in hosts.split(",") if h.strip()) if hosts else None)

    # ------------------------------------------------------------ metadata
    @property
    def resource(self) -> str:
        return f"{self.base}/mcp"

    def resource_metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.base],
                "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]}

    def server_metadata(self) -> dict:
        return {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/oauth/authorize",
            "token_endpoint": f"{self.base}/oauth/token",
            "registration_endpoint": f"{self.base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post",
                                                      "client_secret_basic"],
            "scopes_supported": [SCOPE],
        }

    # ------------------------------------------------------------- storage
    def _load(self) -> dict:
        if not self.path.exists():
            return {"clients": {}, "codes": {}, "access": {}, "refresh": {}}
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        now = time.time()
        for key in ("codes", "access", "refresh"):
            data[key] = {k: v for k, v in data[key].items() if v["exp"] > now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-oauth-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -------------------------------------------------------- registration
    def _redirect_ok(self, uri: str) -> bool:
        try:
            u = urlparse(uri)
        except ValueError:
            return False
        host = (u.hostname or "").lower()
        if u.fragment or not host:
            return False
        if host in ("localhost", "127.0.0.1"):
            return u.scheme in ("http", "https") and host in self.allowed_hosts
        return u.scheme == "https" and any(
            host == h or host.endswith("." + h) for h in self.allowed_hosts)

    def register(self, payload: dict) -> dict:
        uris = payload.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris is required")
        bad = [u for u in uris if not self._redirect_ok(u)]
        if bad:
            raise OAuthError("invalid_redirect_uri", f"redirect host not allowed: {bad[0]}")
        method = payload.get("token_endpoint_auth_method") or "none"
        if method not in self.server_metadata()["token_endpoint_auth_methods_supported"]:
            raise OAuthError("invalid_client_metadata", "unsupported auth method")
        client = {"client_id": secrets.token_urlsafe(16), "redirect_uris": uris,
                  "client_name": str(payload.get("client_name") or "MCP client")[:100],
                  "token_endpoint_auth_method": method,
                  "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"],
                  "client_id_issued_at": int(time.time())}
        stored = dict(client)
        if method != "none":
            client["client_secret"] = secrets.token_urlsafe(32)
            client["client_secret_expires_at"] = 0
            stored["secret_hash"] = _hash(client["client_secret"])
        with self._lock:
            data = self._load()
            if len(data["clients"]) >= MAX_CLIENTS:   # oldest out; bounds the file
                oldest = min(data["clients"], key=lambda k: data["clients"][k]["client_id_issued_at"])
                del data["clients"][oldest]
            data["clients"][client["client_id"]] = stored
            self._save(data)
        logger.info("Registered OAuth client %r", client["client_name"])
        return client

    def client(self, client_id: str) -> Optional[dict]:
        return self._load()["clients"].get(client_id or "")

    # ------------------------------------------------------------ authorize
    def check_authorize(self, p: dict) -> dict:
        """Validate an authorization request. Errors here are shown, never redirected."""
        client = self.client(p.get("client_id", ""))
        if not client:
            raise OAuthError("invalid_client", "Unknown client_id. Re-add the connector.")
        if p.get("redirect_uri") not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "redirect_uri does not match registration.")
        if p.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "response_type must be code.")
        if p.get("code_challenge_method") != "S256" or not p.get("code_challenge"):
            raise OAuthError("invalid_request", "PKCE with S256 is required.")
        return client

    def locked_out(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW]
        self._failures[ip] = recent
        return len(recent) >= MAX_FAILURES

    def approve(self, p: dict, password: str, ip: str) -> str:
        """Check the owner secret and return the redirect URL carrying the code."""
        client = self.check_authorize(p)
        if self.locked_out(ip):
            raise OAuthError("access_denied", "Too many attempts. Try again in 15 minutes.", 429)
        if not hmac.compare_digest(password.encode(), self._secret.encode()):
            self._failures.setdefault(ip, []).append(time.time())
            raise OAuthError("access_denied", "Wrong password.", 401)
        self._failures.pop(ip, None)
        code = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            data["codes"][_hash(code)] = {
                "client_id": client["client_id"], "redirect_uri": p["redirect_uri"],
                "challenge": p["code_challenge"], "exp": time.time() + CODE_TTL}
            self._save(data)
        q = {"code": code}
        if p.get("state"):
            q["state"] = p["state"]
        sep = "&" if urlparse(p["redirect_uri"]).query else "?"
        return p["redirect_uri"] + sep + urlencode(q)

    # ---------------------------------------------------------------- token
    def _client_auth(self, form: dict, basic: Optional[tuple[str, str]]) -> dict:
        cid, secret = (basic if basic else (form.get("client_id", ""),
                                             form.get("client_secret")))
        client = self.client(cid)
        if not client:
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["token_endpoint_auth_method"] != "none":
            if not secret or not hmac.compare_digest(_hash(secret), client["secret_hash"]):
                raise OAuthError("invalid_client", "bad client credentials", 401)
        return client

    def _issue(self, data: dict, client_id: str) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        data["access"][_hash(access)] = {"client_id": client_id, "exp": now + ACCESS_TTL}
        data["refresh"][_hash(refresh)] = {"client_id": client_id, "exp": now + REFRESH_TTL}
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": SCOPE}

    def token(self, form: dict, basic: Optional[tuple[str, str]] = None) -> dict:
        client = self._client_auth(form, basic)
        grant = form.get("grant_type")
        with self._lock:
            data = self._load()
            if grant == "authorization_code":
                rec = data["codes"].pop(_hash(form.get("code", "")), None)   # single use
                self._save(data)
                if (not rec or rec["exp"] < time.time()
                        or rec["client_id"] != client["client_id"]
                        or rec["redirect_uri"] != form.get("redirect_uri")):
                    raise OAuthError("invalid_grant", "code is invalid or expired")
                verifier = form.get("code_verifier", "")
                if not verifier or not hmac.compare_digest(_s256(verifier), rec["challenge"]):
                    raise OAuthError("invalid_grant", "PKCE verification failed")
            elif grant == "refresh_token":
                rec = data["refresh"].pop(_hash(form.get("refresh_token", "")), None)  # rotate
                self._save(data)
                if not rec or rec["exp"] < time.time() or rec["client_id"] != client["client_id"]:
                    raise OAuthError("invalid_grant", "refresh token is invalid or expired")
            else:
                raise OAuthError("unsupported_grant_type", "unsupported grant_type")
            out = self._issue(data, client["client_id"])
            self._save(data)
        return out

    def verify(self, access_token: str) -> bool:
        rec = self._load()["access"].get(_hash(access_token))
        return bool(rec and rec["exp"] > time.time())


# ------------------------------------------------------------------- HTTP
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to HoodaRun</title><style>
body{{font:16px -apple-system,system-ui,sans-serif;background:#0b0d10;color:#e8eaed;
display:grid;place-items:center;min-height:100vh;margin:0}}
form{{background:#16191e;padding:28px;border-radius:16px;width:min(360px,90vw)}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:#9aa0a6;font-size:14px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #333;
background:#0b0d10;color:inherit;font-size:16px}}
button{{margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;
background:#ff6b2c;color:#fff;font-weight:600;font-size:16px}}.err{{color:#ff8a80}}
</style></head><body><form method="post" action="/oauth/authorize">
<h1>Connect {client} to HoodaRun</h1>
<p>It will be able to read your training and publish workouts to your Apple Watch feed.</p>
{error}{hidden}
<input type="password" name="password" placeholder="Server password" autofocus required
autocomplete="current-password">
<button type="submit">Allow</button></form></body></html>"""

_AUTH_FIELDS = ("client_id", "redirect_uri", "response_type", "state", "code_challenge",
                "code_challenge_method", "scope", "resource")


def _flat(qs: dict) -> dict:
    return {k: v[0] for k, v in qs.items() if v}


class OAuthRoutes:
    """Public OAuth endpoints, mounted OUTSIDE the bearer gate."""

    def __init__(self, app, provider: OAuthProvider):
        self.app = app
        self.p = provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope.get("path", ""), scope.get("method", "GET")
        try:
            if path.startswith("/.well-known/oauth-protected-resource") and method == "GET":
                return await self._json(send, 200, self.p.resource_metadata())
            if path.startswith("/.well-known/oauth-authorization-server") and method == "GET":
                return await self._json(send, 200, self.p.server_metadata())
            if path == "/oauth/register" and method == "POST":
                body = await self._body(receive)
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    raise OAuthError("invalid_client_metadata", "body must be JSON")
                return await self._json(send, 201, self.p.register(payload))
            if path == "/oauth/authorize" and method == "GET":
                params = _flat(parse_qs(scope.get("query_string", b"").decode()))
                return await self._page(send, params)
            if path == "/oauth/authorize" and method == "POST":
                form = _flat(parse_qs((await self._body(receive)).decode()))
                ip = (scope.get("client") or ("?",))[0]
                try:
                    url = self.p.approve(form, form.get("password", ""), ip)
                except OAuthError as e:
                    if e.error == "access_denied":
                        return await self._page(send, form, e.description, e.status)
                    raise
                return await self._redirect(send, url)
            if path == "/oauth/token" and method == "POST":
                form = _flat(parse_qs((await self._body(receive)).decode()))
                return await self._json(send, 200, self.p.token(form, self._basic(scope)))
        except OAuthError as e:
            if path == "/oauth/authorize":
                return await self._page(send, {}, e.description, e.status, show_form=False)
            return await self._json(send, e.status,
                                    {"error": e.error, "error_description": e.description})
        await self.app(scope, receive, send)

    @staticmethod
    def _basic(scope) -> Optional[tuple[str, str]]:
        raw = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if not raw.lower().startswith("basic "):
            return None
        try:
            cid, _, secret = base64.b64decode(raw[6:]).decode().partition(":")
        except ValueError:
            return None
        return cid, secret

    @staticmethod
    async def _body(receive) -> bytes:
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_BODY:
                raise OAuthError("invalid_request", "request too large", 413)
        return body

    async def _page(self, send, params: dict, error: str = "", status: int = 200,
                    show_form: bool = True):
        if show_form:
            client = self.p.check_authorize(params)   # raises -> shown as an error page
            hidden = "".join(
                f'<input type="hidden" name="{f}" value="{html.escape(params[f])}">'
                for f in _AUTH_FIELDS if params.get(f))
            name = client["client_name"]
        else:
            hidden, name = "", "this app"
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        page = _PAGE.format(client=html.escape(name), error=err, hidden=hidden)
        if not show_form:
            page = page.split("<input", 1)[0] + "</form></body></html>"
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store"),
            (b"x-frame-options", b"DENY"),
            (b"content-security-policy",
             b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https:; "
             b"frame-ancestors 'none'")]})
        await send({"type": "http.response.body", "body": page.encode()})

    @staticmethod
    async def _redirect(send, url: str):
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", url.encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _json(send, code: int, payload: dict):
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
