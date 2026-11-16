# Accounts: more than one runner on a server

By default the server is single-owner: one password, one feed, one calendar.
Set an invite code and it also takes **members** — each with their own
sign-in, their own workout feed, their own calendar link and their own
HoodaRun app token. Nobody's tools, feed or calendar can reach anyone else's.

```
                    ┌─ owner ── workouts.json ───────────── /calendar/<CALENDAR_TOKEN>.ics
Claude / ChatGPT ──▶│
   (per person)     ├─ alice ── users/<id>/workouts.json ── /calendar/<her token>.ics
                    └─ bob ──── users/<id>/workouts.json ── /calendar/<his token>.ics
```

## Turn it on

| Variable | What it does |
|---|---|
| `SIGNUP_INVITE_CODE` | Opens sign-up to anyone who has this code. Unset = sign-up closed. |
| `OWNER_EMAIL` | Optional. Lets the owner sign in with this email + the server password instead of leaving the email blank. |
| `APPLE_CLIENT_ID`, `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` | Optional. All four turn on Sign in with Apple (below). |

Requires `PUBLIC_BASE_URL` (the pages live on the OAuth host) and a persistent
volume for `WORKOUT_FEED_DIR`: accounts are in `users.json` there, feeds under
`users/`.

Nothing changes for the owner. The server password still signs you in to a
connector (leave the email blank), HoodaRun still uses `MCP_AUTH_TOKEN`, your
calendar is still `/calendar/<CALENDAR_TOKEN>.ics`, and your feed is still
`workouts.json`.

## What a member does

1. **Add the connector.** In Claude or ChatGPT, add a custom connector with
   `https://<host>/mcp`. On the sign-in page choose **Create an account**,
   enter name, email, a password (10+ characters) and the invite code. They
   land back in the connector, signed in. Later sign-ins use email + password.
2. **Subscribe the calendar.** Ask the assistant "what's my calendar
   subscription URL?", or open `https://<host>/account`. Add the `webcal://`
   link once in Apple Calendar; every plan they publish then shows up on all
   their Apple devices.
3. **HoodaRun (optional, for the Watch).** On `https://<host>/account`, press
   **Generate an app token** and paste it with the server URL into HoodaRun ›
   Claude plans. The token is shown once; generating a new one replaces it.

The account page also replaces a leaked calendar link.

## Sign in with Apple

Adds an Apple button beside the password form. It needs an Apple Developer
Program membership:

1. Certificates, Identifiers & Profiles › Identifiers › **Services IDs**: create
   one (this is `APPLE_CLIENT_ID`), enable Sign in with Apple, add your host as
   a domain and `https://<host>/oauth/apple/callback` as a return URL.
2. **Keys**: create a key with Sign in with Apple enabled. Its id is
   `APPLE_KEY_ID`; paste the downloaded `.p8` contents into
   `APPLE_PRIVATE_KEY` (literal `\n` for newlines is fine).
3. `APPLE_TEAM_ID` is the 10-character id at the top right of the portal.

The first time an Apple account signs in it is asked for the invite code;
after that it goes straight through. An Apple sign-in whose email already
belongs to a password account is refused rather than merged, because emails
aren't verified at password sign-up.

**What it is not:** Sign in with Apple proves who someone is. It gives this
server no access to their iCloud or calendar — Apple has no such API. The
calendar still arrives by subscription link or through HoodaRun.

This flow is tested against a stand-in for Apple (`tests/test_apple_signin.py`),
not against Apple itself. Try it with your own Services ID before relying on it.

## Limits worth knowing

- **Strava is the owner's.** One Strava connection per server. Members don't
  need it: with **Share completed workouts** switched on in HoodaRun, their
  own watch data drives fitness assessment and plan mileage. Without it they
  give their weekly mileage and a recent race time instead.
- **No email is ever sent.** No verification, no "forgot password" link. The
  owner resets a password from the server's shell:

  ```
  apple-workouts-accounts list
  apple-workouts-accounts reset-password someone@example.com
  apple-workouts-accounts remove someone@example.com   # leaves their feed folder
  ```
  (on Railway: `railway run apple-workouts-accounts list`, or `railway ssh`).
- **Lockout:** five wrong passwords lock that account's sign-in for 15
  minutes. Existing connectors and tokens keep working.
- **Storage** is JSON files, sized for a club or a group of friends (capped at
  200 members), not a public service.
- Passwords are stored as scrypt hashes; app tokens and OAuth tokens as
  SHA-256 hashes. Calendar tokens are stored as-is — they are read-only links.
