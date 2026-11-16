"""Per-person accounts: each runner gets their own feed, calendar and tokens.

The server started life single-owner: one password, one `workouts.json`, one
calendar. Accounts keep that working unchanged and add members beside it:

    <WORKOUT_FEED_DIR>/
        workouts.json, status.json      the owner's feed (as before)
        oauth.json                      connector sign-ins (as before)
        users.json                      member accounts (mode 600)
        users/<id>/workouts.json …      one feed per member

The **owner** is not a row in `users.json`. They sign in with the server
password (`OAUTH_OWNER_PASSWORD`, default `MCP_AUTH_TOKEN`), HoodaRun keeps
using `MCP_AUTH_TOKEN`, and their calendar keeps using `CALENDAR_TOKEN`.

A **member** signs up with an email, a password and the invite code
(`SIGNUP_INVITE_CODE`; sign-up is closed while it is unset), or with Sign in
with Apple (see apple_signin.py) plus the same invite code. Each member has:

  * a password (scrypt) and/or an Apple account id,
  * a calendar token — the secret in their own `/calendar/<token>.ics` URL,
  * optionally an app token — the bearer HoodaRun on *their* iPhone sends.
    Only its SHA-256 is stored; it is shown once when generated.

There is no email verification and no reset-by-email: the server sends no
mail. The owner resets a forgotten password with the `apple-workouts-accounts`
command (see `main()` below).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .feed import FeedStore

OWNER = "owner"
USERS_FILE = "users.json"
MAX_USERS = 200
MIN_PASSWORD = 10
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _scrypt(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def _now() -> int:
    return int(time.time())


class AccountError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass(frozen=True)
class Identity:
    id: str
    name: str
    email: Optional[str]
    calendar_token: Optional[str]

    @property
    def is_owner(self) -> bool:
        return self.id == OWNER


class AccountStore:
    def __init__(self, root: Path, owner_secret: str, *,
                 owner_app_token: Optional[str] = None,
                 owner_calendar_token: Optional[str] = None,
                 owner_email: Optional[str] = None,
                 invite_code: Optional[str] = None):
        if not owner_secret:
            raise ValueError("accounts need OAUTH_OWNER_PASSWORD or MCP_AUTH_TOKEN")
        self.root = Path(root)
        self.path = self.root / USERS_FILE
        self._owner_secret = owner_secret
        self._owner_app_token = owner_app_token
        self._owner_calendar_token = owner_calendar_token
        self._owner_email = (owner_email or "").strip().lower() or None
        self._invite = invite_code or None
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, root: Path, token: Optional[str]) -> "AccountStore":
        env = os.environ.get
        return cls(root, env("OAUTH_OWNER_PASSWORD") or token or "",
                   owner_app_token=token, owner_calendar_token=env("CALENDAR_TOKEN"),
                   owner_email=env("OWNER_EMAIL"), invite_code=env("SIGNUP_INVITE_CODE"))

    # ------------------------------------------------------------- storage
    def _load(self) -> dict:
        if not self.path.exists():
            return {"users": {}}
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-users-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --------------------------------------------------------------- reads
    @property
    def signup_open(self) -> bool:
        return self._invite is not None

    def store_for(self, user_id: str) -> FeedStore:
        """The feed that belongs to this person. The owner's is the root."""
        if user_id == OWNER:
            return FeedStore(self.root)
        if not re.fullmatch(r"[0-9a-f]{16}", user_id or ""):
            raise AccountError("unknown account", 404)   # never build a path from junk
        return FeedStore(self.root / "users" / user_id)

    def identity(self, user_id: str) -> Optional[Identity]:
        if user_id == OWNER:
            return Identity(OWNER, "Owner", self._owner_email, self._owner_calendar_token)
        user = self._load()["users"].get(user_id or "")
        return self._identity(user) if user else None

    @staticmethod
    def _identity(user: dict) -> Identity:
        return Identity(user["id"], user.get("name") or user["email"], user["email"],
                        user["calendar_token"])

    def by_app_token(self, token: str) -> Optional[str]:
        """Member whose HoodaRun app token this is (the owner's is MCP_AUTH_TOKEN)."""
        if not token:
            return None
        want = _hash(token)
        for user in self._load()["users"].values():
            if user.get("app_token_hash") and hmac.compare_digest(user["app_token_hash"], want):
                return user["id"]
        return None

    def by_calendar_token(self, token: str) -> Optional[str]:
        if not token:
            return None
        if self._owner_calendar_token and hmac.compare_digest(
                token.encode(), self._owner_calendar_token.encode()):
            return OWNER
        for user in self._load()["users"].values():
            if hmac.compare_digest(user["calendar_token"].encode(), token.encode()):
                return user["id"]
        return None

    def by_apple_sub(self, sub: str) -> Optional[str]:
        for user in self._load()["users"].values():
            if sub and user.get("apple_sub") == sub:
                return user["id"]
        return None

    def members(self) -> list[dict]:
        return [{"id": u["id"], "email": u["email"], "name": u.get("name"),
                 "created_at": u["created_at"], "apple": bool(u.get("apple_sub")),
                 "password": bool(u.get("password")), "app_token": bool(u.get("app_token_hash"))}
                for u in self._load()["users"].values()]

    # ------------------------------------------------------------- sign-up
    def check_invite(self, invite: str) -> None:
        if not self._invite:
            raise AccountError("Sign-up is closed on this server.", 403)
        if not hmac.compare_digest((invite or "").strip().encode(), self._invite.encode()):
            raise AccountError("That invite code isn't right.", 403)

    def create(self, email: str, password: Optional[str], name: str = "",
               invite: str = "", apple_sub: Optional[str] = None,
               ip: str = "?") -> Identity:
        """Create a member. Needs the invite code and a password or an Apple id."""
        if self._locked(f"signup|{ip}"):
            raise AccountError("Too many attempts. Try again in 15 minutes.", 429)
        try:
            self.check_invite(invite)
        except AccountError:
            self._fail(f"signup|{ip}")
            raise
        email = (email or "").strip().lower()
        if not _EMAIL.match(email):
            raise AccountError("Enter a valid email address.")
        if email == self._owner_email:
            raise AccountError("That email already has an account. Sign in instead.", 409)
        if password is None and not apple_sub:
            raise AccountError("A password is required.")
        if password is not None:
            self._check_password(password)
        with self._lock:
            data = self._load()
            if any(u["email"] == email for u in data["users"].values()):
                raise AccountError("That email already has an account. Sign in instead.", 409)
            if len(data["users"]) >= MAX_USERS:
                raise AccountError("This server isn't taking new accounts.", 403)
            uid = secrets.token_hex(8)
            user = {"id": uid, "email": email, "name": (name or "").strip()[:80],
                    "created_at": _now(), "calendar_token": secrets.token_urlsafe(24)}
            if password is not None:
                user["password"] = self._password_record(password)
            if apple_sub:
                user["apple_sub"] = apple_sub
            data["users"][uid] = user
            self._save(data)
        return self._identity(user)

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AccountError(f"Use a password of at least {MIN_PASSWORD} characters.")
        if len(password) > 256:
            raise AccountError("That password is too long.")

    @staticmethod
    def _password_record(password: str) -> dict:
        salt = secrets.token_bytes(16)
        return {"salt": salt.hex(), "hash": _scrypt(password, salt)}

    # ------------------------------------------------------------- sign-in
    def _locked(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < FAILURE_WINDOW]
        self._failures[key] = recent
        return len(recent) >= MAX_FAILURES

    def _fail(self, key: str) -> None:
        self._failures.setdefault(key, []).append(time.time())

    def authenticate(self, email: str, password: str, ip: str = "?") -> Identity:
        """Owner: the server password, with the email blank (or OWNER_EMAIL).
        Member: their email and password. Lockout is per caller and per account,
        so guessing one person's password can't lock everyone else out."""
        email = (email or "").strip().lower()
        keys = [f"ip|{ip}", f"acct|{email or OWNER}"]
        if any(self._locked(k) for k in keys):
            raise AccountError("Too many attempts. Try again in 15 minutes.", 429)
        ident = self._check_credentials(email, password or "")
        if ident is None:
            for k in keys:
                self._fail(k)
            raise AccountError("Wrong password." if not email else "Wrong email or password.",
                               401)
        for k in keys:
            self._failures.pop(k, None)
        return ident

    def _check_credentials(self, email: str, password: str) -> Optional[Identity]:
        if not email or email == self._owner_email:
            ok = hmac.compare_digest(password.encode(), self._owner_secret.encode())
            return self.identity(OWNER) if ok else None
        user = next((u for u in self._load()["users"].values() if u["email"] == email), None)
        record = (user or {}).get("password")
        if not record:
            # Burn the same time as a real check so a miss doesn't reveal which
            # emails have accounts.
            _scrypt(password, b"\0" * 16)
            return None
        ok = hmac.compare_digest(_scrypt(password, bytes.fromhex(record["salt"])),
                                 record["hash"])
        return self._identity(user) if ok else None

    # ------------------------------------------------------------ mutation
    def _update(self, user_id: str, change) -> dict:
        with self._lock:
            data = self._load()
            user = data["users"].get(user_id)
            if not user:
                raise AccountError("unknown account", 404)
            change(user)
            self._save(data)
            return user

    def new_app_token(self, user_id: str) -> str:
        """Mint (or replace) the bearer HoodaRun uses for this member. Shown once."""
        if user_id == OWNER:
            raise AccountError("The owner's app token is MCP_AUTH_TOKEN.")
        token = "hr_" + secrets.token_urlsafe(32)
        self._update(user_id, lambda u: u.__setitem__("app_token_hash", _hash(token)))
        return token

    def rotate_calendar_token(self, user_id: str) -> str:
        if user_id == OWNER:
            raise AccountError("Rotate the owner's calendar URL by changing CALENDAR_TOKEN.")
        token = secrets.token_urlsafe(24)
        self._update(user_id, lambda u: u.__setitem__("calendar_token", token))
        return token

    def set_password(self, email: str, password: str) -> None:
        self._check_password(password)
        uid = self._id_for(email)
        self._update(uid, lambda u: u.__setitem__("password", self._password_record(password)))

    def remove(self, email: str) -> str:
        """Delete the account row. The feed directory is left for the owner to
        archive or delete; returns its path."""
        uid = self._id_for(email)
        with self._lock:
            data = self._load()
            data["users"].pop(uid, None)
            self._save(data)
        return str(self.root / "users" / uid)

    def _id_for(self, email: str) -> str:
        email = (email or "").strip().lower()
        for user in self._load()["users"].values():
            if user["email"] == email:
                return user["id"]
        raise AccountError(f"no account for {email}", 404)


# ----------------------------------------------------------------- owner CLI
def main(argv: Optional[list[str]] = None) -> int:
    """apple-workouts-accounts list | reset-password EMAIL | remove EMAIL

    Run it where the server's data lives (WORKOUT_FEED_DIR must point at the
    same directory), e.g. `railway run apple-workouts-accounts list`.
    """
    import getpass
    import sys

    from .feed import default_dir

    args = list(sys.argv[1:] if argv is None else argv)
    store = AccountStore(default_dir(), os.environ.get("OAUTH_OWNER_PASSWORD")
                         or os.environ.get("MCP_AUTH_TOKEN") or "unused")
    try:
        if args[:1] == ["list"]:
            rows = store.members()
            for r in rows:
                how = "+".join(k for k in ("password", "apple") if r[k]) or "no sign-in"
                print(f"{r['email']:<40} {r['name'] or '':<24} {how:<16} {r['id']}")
            print(f"{len(rows)} member(s) in {store.path}")
            return 0
        if args[:1] == ["reset-password"] and len(args) == 2:
            store.set_password(args[1], getpass.getpass("New password: "))
            print("Password updated.")
            return 0
        if args[:1] == ["remove"] and len(args) == 2:
            print("Removed. Their workouts are still in", store.remove(args[1]))
            return 0
    except AccountError as exc:
        print("error:", exc.message, file=sys.stderr)
        return 1
    print(main.__doc__, file=sys.stderr)
    return 2
