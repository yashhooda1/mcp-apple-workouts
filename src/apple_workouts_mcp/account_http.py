"""Sign-up, the account page, and Sign in with Apple — public pages, mounted
OUTSIDE the bearer gate next to the OAuth endpoints.

    GET/POST /signup                 create an account (invite code required)
    GET/POST /account                sign in to see your own setup details
    GET      /oauth/apple/start      -> Apple
    POST     /oauth/apple/callback   <- Apple (form_post)
    POST     /oauth/apple/finish     first Apple sign-in: invite code

Two ways in, one way out. A request that carries a connector's authorization
parameters (someone clicked "Create an account" or "Sign in with Apple" on the
connector sign-in page) ends with a redirect back to that connector, signed
in. Without them it ends on the account page, which shows the three things a
person needs: the connector URL, their calendar subscription URL, and a
HoodaRun app token.

No cookies and no sessions. After a sign-in the account page carries a
short-lived ticket — an HMAC over the account id and an expiry, keyed with a
secret that lives only in this process — so its buttons work without asking
for the password again.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode

from . import pages
from .accounts import AccountError, AccountStore
from .oauth import MAX_BODY, OAuthError, OAuthProvider, auth_params

TICKET_TTL = 600
PENDING_TTL = 600
MAX_PENDING = 500
_SECRET = secrets.token_bytes(32)   # a restart signs everyone out of /account


def _ticket(user_id: str) -> str:
    exp = str(int(time.time()) + TICKET_TTL)
    mac = hmac.new(_SECRET, f"{user_id}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{exp}.{mac}"


def _ticket_user(ticket: str) -> Optional[str]:
    try:
        user_id, exp, mac = (ticket or "").split(".")
        good = hmac.new(_SECRET, f"{user_id}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(mac, good) and int(exp) > time.time():
            return user_id
    except ValueError:
        pass
    return None


def _flat(qs: dict) -> dict:
    return {k: v[0] for k, v in qs.items() if v}


class AccountRoutes:
    def __init__(self, app, provider: OAuthProvider, apple=None):
        self.app = app
        self.p = provider
        self.accounts: AccountStore = provider.accounts
        self.apple = apple
        self._pending: dict[str, dict] = {}   # first-time Apple sign-ins awaiting an invite

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        route = {
            ("/signup", "GET"): self._signup_form, ("/signup", "POST"): self._signup,
            ("/account", "GET"): self._account_form, ("/account", "POST"): self._account,
            ("/oauth/apple/start", "GET"): self._apple_start,
            ("/oauth/apple/callback", "POST"): self._apple_callback,
            ("/oauth/apple/finish", "POST"): self._apple_finish,
        }.get((path, method))
        if route is None:
            return await self.app(scope, receive, send)
        try:
            query = _flat(parse_qs(scope.get("query_string", b"").decode()))
            form = _flat(parse_qs((await self._body(receive)).decode())) \
                if method == "POST" else {}
            ip = pages.client_ip(scope)
            await route(send, query, form, ip)
        except (AccountError, OAuthError) as e:
            message = e.message if isinstance(e, AccountError) else e.description
            await pages.send_page(send, "HoodaRun", "<h1>That didn't work</h1>"
                                  + pages.error(message)
                                  + '<a class="btn quiet" href="/account">Back to sign-in</a>',
                                  e.status)

    @staticmethod
    async def _body(receive) -> bytes:
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_BODY:
                raise AccountError("Request too large.", 413)
        return body

    # ---------------------------------------------------------------- finish
    async def _finish(self, send, carry: dict, user_id: str, notice: str = "") -> None:
        """Signed in. Back to the connector if one is waiting, else the account page."""
        if carry.get("client_id"):
            return await pages.redirect(send, self.p.grant(carry, user_id))
        await self._account_page(send, user_id, notice)

    # --------------------------------------------------------------- sign-up
    async def _signup_form(self, send, query, form, ip, message: str = "",
                           status: int = 200, values: Optional[dict] = None):
        carry = auth_params(values if values is not None else query)
        if carry.get("client_id"):
            self.p.check_authorize(carry)   # don't collect a password for a bogus request
        if not self.accounts.signup_open:
            raise AccountError("Sign-up is closed on this server.", 403)
        v = values or {}
        body = (
            "<h1>Create your HoodaRun account</h1>"
            "<p>Your training plans, calendar and Apple Watch feed are yours alone.</p>"
            + pages.error(message)
            + '<form method="post" action="/signup">' + pages.hidden(carry)
            + f'<label for="name">Name</label><input id="name" name="name" maxlength="80" '
              f'value="{pages.esc(v.get("name", ""))}" autocomplete="name">'
            + f'<label for="email">Email</label><input id="email" type="email" name="email" '
              f'required value="{pages.esc(v.get("email", ""))}" autocomplete="username">'
            + '<label for="pw">Password (10+ characters)</label><input id="pw" type="password" '
              'name="password" required minlength="10" autocomplete="new-password">'
            + '<label for="invite">Invite code</label><input id="invite" name="invite" '
              'required autocomplete="off">'
            + '<button type="submit">Create account</button></form>')
        if self.apple:
            body += ('<p class="or">or</p><a class="btn alt" href="/oauth/apple/start?'
                     + pages.esc(urlencode(carry)) + '"> Sign up with Apple</a>')
        await pages.send_page(send, "Create your HoodaRun account", body, status)

    async def _signup(self, send, query, form, ip):
        carry = auth_params(form)
        if carry.get("client_id"):
            self.p.check_authorize(carry)   # before creating anything
        try:
            who = self.accounts.create(form.get("email", ""), form.get("password", ""),
                                       form.get("name", ""), form.get("invite", ""), ip=ip)
        except AccountError as e:
            if e.status == 429:
                raise
            return await self._signup_form(send, query, form, ip, e.message, e.status, form)
        await self._finish(send, carry, who.id, "Account created.")

    # --------------------------------------------------------------- account
    async def _account_form(self, send, query, form, ip, message: str = "",
                            status: int = 200):
        body = ("<h1>Your HoodaRun account</h1>"
                "<p>Sign in to see your calendar link and app token.</p>"
                + pages.error(message)
                + '<form method="post" action="/account">'
                  '<label for="email">Email <span>(leave blank if you own this server)</span>'
                  '</label><input id="email" type="email" name="email" autocomplete="username">'
                  '<label for="pw">Password</label><input id="pw" type="password" '
                  'name="password" required autocomplete="current-password">'
                  '<button type="submit">Sign in</button></form>')
        if self.apple:
            body += ('<p class="or">or</p><a class="btn alt" href="/oauth/apple/start">'
                     " Sign in with Apple</a>")
        if self.accounts.signup_open:
            body += '<p class="or">New here? <a href="/signup">Create an account</a>.</p>'
        await pages.send_page(send, "Your HoodaRun account", body, status)

    async def _account(self, send, query, form, ip):
        if form.get("ticket"):
            user_id = _ticket_user(form["ticket"])
            if not user_id or not self.accounts.identity(user_id):
                return await self._account_form(send, query, form, ip,
                                                "That page expired. Sign in again.", 401)
            action = form.get("action")
            if action == "app_token":
                token = self.accounts.new_app_token(user_id)
                return await self._account_page(send, user_id, app_token=token)
            if action == "rotate_calendar":
                self.accounts.rotate_calendar_token(user_id)
                return await self._account_page(
                    send, user_id, "New calendar link created. The old one no longer "
                    "works — subscribe again with the new link.")
            return await self._account_page(send, user_id)
        try:
            who = self.accounts.authenticate(form.get("email", ""),
                                             form.get("password", ""), ip)
        except AccountError as e:
            return await self._account_form(send, query, form, ip, e.message, e.status)
        await self._account_page(send, who.id)

    async def _account_page(self, send, user_id: str, notice: str = "",
                            app_token: str = ""):
        who = self.accounts.identity(user_id)
        if who is None:
            raise AccountError("unknown account", 404)
        base = self.p.base
        host = base.split("://", 1)[-1]
        ticket = pages.hidden({"ticket": _ticket(user_id)})
        body = [f"<h1>{pages.esc(who.name)}</h1>",
                f"<p>{pages.esc(who.email or 'Server owner')}</p>",
                f'<p class="ok">{pages.esc(notice)}</p>' if notice else "",
                "<h2>1 · Claude or ChatGPT</h2><p>Add a custom connector with this URL, "
                f"then sign in with this account.</p><code>{pages.esc(base)}/mcp</code>",
                "<h2>2 · Apple Calendar</h2>"]
        if who.calendar_token:
            body.append(
                "<p>Subscribe once and every plan you publish shows up on all your Apple "
                "devices. On iPhone: Settings › Apps › Calendar › Calendar Accounts › "
                "Add Account › Other › Add Subscribed Calendar. Treat the link like a "
                "password.</p>"
                f"<code>webcal://{pages.esc(host)}/calendar/{pages.esc(who.calendar_token)}.ics"
                "</code>")
            if not who.is_owner:
                body.append('<form method="post" action="/account">' + ticket
                            + pages.hidden({"action": "rotate_calendar"})
                            + '<button class="quiet" type="submit">Replace this link'
                              "</button></form>")
        else:
            body.append("<p>Set CALENDAR_TOKEN on the server to enable your calendar "
                        "link.</p>")
        body.append("<h2>3 · HoodaRun on your iPhone</h2>")
        if who.is_owner:
            body.append("<p>In HoodaRun › Claude plans, use this server URL and your "
                        f"MCP_AUTH_TOKEN.</p><code>{pages.esc(base)}</code>")
        else:
            body.append("<p>In HoodaRun › Claude plans, enter this server URL and an app "
                        f"token.</p><code>{pages.esc(base)}</code>")
            if app_token:
                body.append('<p class="ok">Your app token — copy it now, it is shown '
                            f"only once.</p><code>{pages.esc(app_token)}</code>")
            body.append('<form method="post" action="/account">' + ticket
                        + pages.hidden({"action": "app_token"})
                        + '<button class="quiet" type="submit">'
                        + ("Generate a new app token" if app_token
                           else "Generate an app token (replaces any earlier one)")
                        + "</button></form>")
        await pages.send_page(send, "Your HoodaRun account", "".join(body))

    # ----------------------------------------------------------------- Apple
    def _need_apple(self):
        if not self.apple:
            raise AccountError("Sign in with Apple isn't set up on this server.", 404)
        return self.apple

    async def _apple_start(self, send, query, form, ip):
        apple = self._need_apple()
        carry = auth_params(query)
        if carry.get("client_id"):
            self.p.check_authorize(carry)
        await pages.redirect(send, apple.begin({"carry": carry}))

    async def _apple_callback(self, send, query, form, ip):
        apple = self._need_apple()
        # The code exchange and key fetch are blocking HTTP calls to Apple.
        payload, who = await asyncio.to_thread(apple.complete, form)
        carry = payload.get("carry") or {}
        user_id = self.accounts.by_apple_sub(who.sub)
        if user_id:
            return await self._finish(send, carry, user_id)
        # First time we've seen this Apple account: it still needs the invite.
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v["exp"] > now}
        if len(self._pending) >= MAX_PENDING:
            raise AccountError("Too many sign-ups in progress. Try again shortly.", 429)
        key = secrets.token_urlsafe(24)
        self._pending[key] = {"carry": carry, "sub": who.sub, "email": who.email,
                              "name": who.name, "exp": now + PENDING_TTL}
        await self._apple_invite_page(send, key)

    async def _apple_invite_page(self, send, key: str, message: str = "",
                                 status: int = 200):
        pending = self._pending[key]
        if not self.accounts.signup_open:
            raise AccountError("Sign-up is closed on this server.", 403)
        email = ("" if pending["email"] else
                 '<label for="email">Email</label><input id="email" type="email" '
                 'name="email" required autocomplete="email">')
        body = ("<h1>One more step</h1><p>This Apple account is new here. Enter your "
                "invite code to create your HoodaRun account.</p>" + pages.error(message)
                + '<form method="post" action="/oauth/apple/finish">'
                + pages.hidden({"pending": key}) + email
                + '<label for="invite">Invite code</label><input id="invite" name="invite" '
                  'required autocomplete="off" autofocus>'
                  '<button type="submit">Create account</button></form>')
        await pages.send_page(send, "Create your HoodaRun account", body, status)

    async def _apple_finish(self, send, query, form, ip):
        self._need_apple()
        key = form.get("pending", "")
        pending = self._pending.get(key)
        if not pending or pending["exp"] < time.time():
            raise AccountError("That Apple sign-in expired. Start again.", 400)
        try:
            who = self.accounts.create(pending["email"] or form.get("email", ""), None,
                                       pending["name"], form.get("invite", ""),
                                       apple_sub=pending["sub"], ip=ip)
        except AccountError as e:
            if e.status in (400, 403) and self.accounts.signup_open:   # fixable: ask again
                return await self._apple_invite_page(send, key, e.message, e.status)
            self._pending.pop(key, None)
            raise
        self._pending.pop(key, None)
        await self._finish(send, pending["carry"], who.id, "Account created.")
