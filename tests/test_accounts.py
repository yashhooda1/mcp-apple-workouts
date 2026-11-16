"""Per-person accounts: sign-up, sign-in, and — above all — that one person's
tokens can only ever reach that person's feed and calendar."""
import base64
import hashlib
import importlib
import json
import re
import stat
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from apple_workouts_mcp.accounts import OWNER, AccountError, AccountStore
from apple_workouts_mcp.accounts import main as accounts_cli

BASE = "https://run.example.com"
CB = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()) \
    .rstrip(b"=").decode()
INVITE = "invite-xyz"
PW = "correct-horse-1"


# ------------------------------------------------------------------ the store
def _store(tmp_path, **kw):
    kw.setdefault("invite_code", INVITE)
    return AccountStore(tmp_path, "owner-pw", owner_app_token="static-tok",
                        owner_calendar_token="owner-cal", **kw)


def test_signup_needs_the_invite_code_and_a_real_password(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(AccountError, match="invite"):
        s.create("a@example.com", PW, invite="nope")
    with pytest.raises(AccountError, match="10 characters"):
        s.create("a@example.com", "short", invite=INVITE)
    with pytest.raises(AccountError, match="valid email"):
        s.create("not-an-email", PW, invite=INVITE)
    who = s.create(" A@Example.com ", PW, "Alice", INVITE)
    assert who.email == "a@example.com" and who.name == "Alice" and not who.is_owner
    with pytest.raises(AccountError) as dup:
        s.create("a@example.com", "another-pass-22", invite=INVITE)
    assert dup.value.status == 409


def test_signup_is_closed_without_an_invite_code(tmp_path):
    s = _store(tmp_path, invite_code=None)
    assert not s.signup_open
    with pytest.raises(AccountError, match="closed"):
        s.create("a@example.com", PW, invite="")


def test_owner_and_member_sign_in(tmp_path):
    s = _store(tmp_path, owner_email="me@example.com")
    alice = s.create("a@example.com", PW, invite=INVITE)
    assert s.authenticate("", "owner-pw").id == OWNER
    assert s.authenticate("me@example.com", "owner-pw").is_owner
    assert s.authenticate("A@example.com", PW).id == alice.id
    with pytest.raises(AccountError) as wrong:
        s.authenticate("a@example.com", "owner-pw")     # owner secret is not a master key
    assert wrong.value.status == 401
    with pytest.raises(AccountError):
        s.authenticate("nobody@example.com", PW)


def test_lockout_is_per_account_not_global(tmp_path):
    s = _store(tmp_path)
    s.create("a@example.com", PW, invite=INVITE)
    for _ in range(5):
        with pytest.raises(AccountError):
            s.authenticate("a@example.com", "guess", ip="6.6.6.6")
    with pytest.raises(AccountError) as locked:
        s.authenticate("a@example.com", PW, ip="1.2.3.4")   # right password, locked account
    assert locked.value.status == 429
    assert s.authenticate("", "owner-pw", ip="1.2.3.4").is_owner   # everyone else unaffected


def test_secrets_are_not_stored_in_the_clear(tmp_path):
    s = _store(tmp_path)
    alice = s.create("a@example.com", PW, invite=INVITE)
    token = s.new_app_token(alice.id)
    raw = s.path.read_text()
    assert PW not in raw and token not in raw
    assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
    assert s.by_app_token(token) == alice.id and s.by_app_token("hr_nope") is None
    assert s.by_app_token(s.new_app_token(alice.id)) == alice.id
    assert s.by_app_token(token) is None                  # the old one is replaced


def test_each_person_has_their_own_feed_and_calendar_token(tmp_path):
    s = _store(tmp_path)
    a = s.create("a@example.com", PW, invite=INVITE)
    b = s.create("b@example.com", PW, invite=INVITE)
    assert s.store_for(OWNER).dir == tmp_path
    assert s.store_for(a.id).dir == tmp_path / "users" / a.id != s.store_for(b.id).dir
    assert s.by_calendar_token("owner-cal") == OWNER
    assert s.by_calendar_token(a.calendar_token) == a.id
    assert s.by_calendar_token(b.calendar_token) == b.id and a.calendar_token != b.calendar_token
    old = a.calendar_token
    new = s.rotate_calendar_token(a.id)
    assert s.by_calendar_token(old) is None and s.by_calendar_token(new) == a.id
    with pytest.raises(AccountError):
        s.store_for("../../etc")                           # ids never become paths unchecked


def test_owner_cli_lists_and_removes(tmp_path, monkeypatch, capsys):
    s = _store(tmp_path)
    s.create("a@example.com", PW, "Alice", INVITE)
    monkeypatch.setenv("WORKOUT_FEED_DIR", str(tmp_path))
    assert accounts_cli(["list"]) == 0 and "a@example.com" in capsys.readouterr().out
    assert accounts_cli(["remove", "a@example.com"]) == 0
    assert s.members() == [] and accounts_cli(["remove", "a@example.com"]) == 1
    assert accounts_cli(["bogus"]) == 2


# ------------------------------------------------- the whole server, over HTTP
@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The real stack (gate + OAuth + accounts + feed + MCP), in process."""
    from apple_workouts_mcp import server
    from apple_workouts_mcp.feed import FeedStore
    from apple_workouts_mcp.http_auth import build_app

    root = tmp_path_factory.mktemp("feed")
    mp = pytest.MonkeyPatch()
    for k, v in {"TRANSPORT": "streamable-http", "HOST": "0.0.0.0", "PUBLIC_BASE_URL": BASE,
                 "OAUTH_OWNER_PASSWORD": "owner-pw", "CALENDAR_TOKEN": "owner-cal",
                 "SIGNUP_INVITE_CODE": INVITE, "WORKOUT_FEED_DIR": str(root)}.items():
        mp.setenv(k, v)
    for k in ("APPLE_CLIENT_ID", "OWNER_EMAIL", "STRAVA_REFRESH_TOKEN"):
        mp.delenv(k, raising=False)
    hosted = importlib.reload(server)      # a fresh FastMCP: its session manager runs once
    app = build_app(hosted.mcp, "streamable-http", "0.0.0.0", "static-tok", FeedStore(root))
    try:
        with TestClient(app, base_url=BASE, follow_redirects=False) as client:
            yield client, root
    finally:
        mp.undo()
        importlib.reload(server)


def _auth_request(c):
    cid = c.post("/oauth/register", json={"client_name": "Claude",
                                          "redirect_uris": [CB]}).json()["client_id"]
    return {"client_id": cid, "redirect_uri": CB, "response_type": "code", "state": "xyz",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256"}


def _token(c, q, response):
    assert response.status_code == 302, response.text
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                     "client_id": q["client_id"], "redirect_uri": CB,
                                     "code_verifier": VERIFIER})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _signup(c, email, name):
    q = _auth_request(c)
    return _token(c, q, c.post("/signup", data={**q, "name": name, "email": email,
                                                "password": PW, "invite": INVITE}))


def _signin(c, email, password):
    q = _auth_request(c)
    return _token(c, q, c.post("/oauth/authorize",
                               data={**q, "email": email, "password": password}))


class Mcp:
    """Just enough of an MCP client: one session, tools/call, parse the reply."""

    def __init__(self, client, token):
        self.c = client
        self.h = {"Authorization": f"Bearer {token}",
                  "accept": "application/json, text/event-stream"}
        r = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}})
        self.h["mcp-session-id"] = r.headers["mcp-session-id"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._id = 0

    def _post(self, body):
        r = self.c.post("/mcp", json=body, headers=self.h)
        assert r.status_code in (200, 202), (r.status_code, r.text)
        return r

    def call(self, tool, **args):
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}})
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
        result = json.loads(text)["result"]
        payload = result["content"][0]["text"]
        return ("error", payload) if result.get("isError") else json.loads(payload)


RUN = {"name": "Easy 4", "steps": [{"kind": "run", "duration": "4mi", "target": "hr:2"}]}


def test_people_are_isolated_end_to_end(site):
    c, root = site
    alice, bob = _signup(c, "alice@example.com", "Alice"), _signup(c, "bob@example.com", "Bob")
    owner = _signin(c, "", "owner-pw")
    assert _signin(c, "alice@example.com", PW)            # she can sign back in later
    mo, ma, mb = Mcp(c, owner), Mcp(c, alice), Mcp(c, bob)

    mo.call("create_workout", spec={**RUN, "name": "Owner run"}, schedule_date="2026-12-01")
    ma.call("create_workout", spec={**RUN, "name": "Alice run"}, schedule_date="2026-12-01")
    ma.call("create_workout", spec={**RUN, "name": "Alice run 2"}, schedule_date="2026-12-02")

    def names(m):
        return [w["name"] for w in m.call("list_scheduled")["workouts"]]

    assert names(mo) == ["Owner run"]
    assert names(ma) == ["Alice run", "Alice run 2"]
    assert names(mb) == []

    # the same isolation on every other door into the feed
    def feed(token):
        r = c.get("/feed.json", headers={"Authorization": f"Bearer {token}"})
        return [w["name"] for w in r.json()["workouts"]]

    assert feed("static-tok") == ["Owner run"] and feed(alice) == ["Alice run", "Alice run 2"]
    urls = [m.call("calendar_subscription")["https_url"] for m in (mo, ma, mb)]
    assert len(set(urls)) == 3 and urls[0] == f"{BASE}/calendar/owner-cal.ics"
    cals = [c.get(urlparse(u).path).text for u in urls]
    assert "Owner run" in cals[0] and "Alice run" not in cals[0]
    assert cals[1].count("BEGIN:VEVENT") == 2 and "Owner run" not in cals[1]
    assert "BEGIN:VEVENT" not in cals[2]
    assert c.get("/calendar/not-a-real-token.ics").status_code == 404

    # a member clearing everything clears only their own
    ma.call("clear_scheduled", start_date="2026-01-01", end_date="2028-01-01", dry_run=False)
    assert names(ma) == [] and names(mo) == ["Owner run"]

    # on disk: the owner's feed is where it always was; members get folders
    users = json.loads((root / "users.json").read_text())["users"]
    assert json.loads((root / "workouts.json").read_text())["workouts"][0]["name"] == "Owner run"
    assert all((root / "users" / uid / "workouts.json").exists()
               for uid, u in users.items() if u["email"] == "alice@example.com")


def test_members_never_see_the_owners_strava(site, monkeypatch):
    c, _ = site
    from apple_workouts_mcp import strava

    monkeypatch.setattr(strava, "list_activities", lambda **kw: [{"name": "owner's run"}])
    owner, carol = _signin(c, "", "owner-pw"), _signup(c, "carol@example.com", "Carol")
    assert Mcp(c, owner).call("list_activities") == {"name": "owner's run"}
    for tool, args in (("list_activities", {}), ("assess_fitness", {}),
                       ("create_marathon_plan", {"race_name": "X", "race_date": "2027-04-18",
                                                 "goal_time": "4:30:00"})):
        kind, message = Mcp(c, carol).call(tool, **args)
        assert kind == "error" and "owner only" in message and "owner's run" not in message
    # with her own mileage the marathon plan builds without any Strava
    out = Mcp(c, carol).call("create_marathon_plan", race_name="X", race_date="2027-04-18",
                             goal_time="4:30:00", current_weekly_miles=20, dry_run=True)
    assert out["sessions"]


def test_account_page_app_token_and_calendar_rotation(site):
    c, _ = site
    page = c.post("/signup", data={"name": "Dan", "email": "dan@example.com",
                                   "password": PW, "invite": INVITE})
    assert page.status_code == 200 and f"{BASE}/mcp" in page.text
    cal = re.search(r"webcal://run\.example\.com(/calendar/[^<]+\.ics)", page.text).group(1)
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text).group(1)
    assert c.get(cal).status_code == 200

    minted = c.post("/account", data={"ticket": ticket, "action": "app_token"})
    token = re.search(r"<code>(hr_[^<]+)</code>", minted.text).group(1)
    assert c.get("/feed.json", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert c.post("/status", json={"synced_at": "now"},
                  headers={"Authorization": f"Bearer {token}"}).status_code == 200

    rotated = c.post("/account", data={"ticket": ticket, "action": "rotate_calendar"})
    new = re.search(r"webcal://run\.example\.com(/calendar/[^<]+\.ics)", rotated.text).group(1)
    assert new != cal and c.get(cal).status_code == 404 and c.get(new).status_code == 200

    assert c.post("/account", data={"ticket": ticket + "0", "action": "app_token"}) \
        .status_code == 401                               # tampered ticket
    assert c.post("/account", data={"email": "dan@example.com", "password": "wrong-pass-1"}) \
        .status_code == 401
    assert "dan@example.com" in c.post(
        "/account", data={"email": "dan@example.com", "password": PW}).text


def test_signup_page_guards(site):
    c, _ = site
    q = _auth_request(c)
    assert "Invite code" in c.get("/signup", params=q).text
    bad = c.post("/signup", data={**q, "email": "eve@example.com", "password": PW,
                                  "invite": "wrong"})
    assert bad.status_code == 403 and "invite" in bad.text and "eve@example.com" in bad.text
    # a sign-up can't be used to mint a code for a redirect the client never registered
    forged = c.post("/signup", data={**q, "redirect_uri": "https://claude.ai/elsewhere",
                                     "email": "eve@example.com", "password": PW,
                                     "invite": INVITE})
    assert forged.status_code == 400 and "location" not in forged.headers
    assert not any(m["email"] == "eve@example.com"
                   for m in AccountStore(site[1], "owner-pw").members())   # nothing created
    assert c.get("/oauth/apple/start").status_code == 404   # Apple not configured here


def test_removed_account_loses_access(site):
    c, root = site
    token = _signup(c, "gone@example.com", "Gone")
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/feed.json", headers=h).status_code == 200
    AccountStore(root, "owner-pw").remove("gone@example.com")
    assert c.get("/feed.json", headers=h).status_code == 401


def test_tokens_from_before_accounts_belong_to_the_owner(tmp_path):
    from apple_workouts_mcp.oauth import OAuthProvider, _hash

    p = OAuthProvider(BASE, "owner-pw", tmp_path / "oauth.json")
    p._save({"clients": {}, "codes": {}, "refresh": {},
             "access": {_hash("legacy"): {"client_id": "c", "exp": time.time() + 60}}})
    assert p.verify("legacy") == OWNER and p.verify("unknown") is None
