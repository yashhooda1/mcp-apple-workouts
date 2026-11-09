# Apple Calendar, Claude and ChatGPT

```
Claude / ChatGPT ──OAuth──▶ /mcp ──▶ workouts.json ──▶ HoodaRun ──▶ Apple Watch (WorkoutKit)
                                          │                └──────▶ Apple Calendar (EventKit, opt-in)
                                          └──▶ /calendar/<token>.ics ──▶ Apple Calendar (subscription)
```

## Server variables

| Variable | Needed for | Notes |
|---|---|---|
| `MCP_AUTH_TOKEN` | everything remote | Static bearer for HoodaRun, Claude Desktop, OpenAI API |
| `PUBLIC_BASE_URL` | Claude/ChatGPT connectors | `https://<host>`, no trailing path. Turns OAuth on |
| `OAUTH_OWNER_PASSWORD` | connector sign-in | Optional; defaults to `MCP_AUTH_TOKEN` |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | locking down clients | Default `claude.ai,claude.com,chatgpt.com,chat.openai.com,localhost,127.0.0.1` |
| `CALENDAR_TOKEN` | calendar subscription | Separate, read-only secret |
| `WORKOUT_FEED_DIR` | persistence | Must be a volume: holds `workouts.json`, `status.json`, `oauth.json` |

## Connect Claude

Claude app (web, desktop or mobile): **Settings › Connectors › Add custom connector**. Paste
`https://<host>/mcp`, leave the OAuth fields empty (Claude registers itself),
and sign in with your server password on the HoodaRun page that opens.

## Connect ChatGPT

In a workspace or plan that allows custom MCP servers, add a custom connector
with `https://<host>/mcp` and OAuth authentication. ChatGPT discovers the
endpoints, registers itself, and opens the same sign-in page.

## What the OAuth server does

- `/.well-known/oauth-protected-resource` (RFC 9728) and
  `/.well-known/oauth-authorization-server` (RFC 8414) for discovery; a 401
  from `/mcp` points clients at them via `WWW-Authenticate: resource_metadata=`.
- `POST /oauth/register` (RFC 7591): only redirect URIs on allow-listed hosts
  (https only, except localhost).
- `/oauth/authorize`: single-owner password page, 5 wrong attempts → 15 min lockout,
  not frameable.
- `/oauth/token`: PKCE S256 required, single-use 5-minute codes, 1-hour access
  tokens, 30-day refresh tokens that rotate on use.
- Only SHA-256 hashes of codes and tokens are written to `oauth.json` (mode 600).
- To sign every connector out, delete `oauth.json` and restart.

## Apple Calendar — two options, pick one

**Subscription (server).** Ask Claude "what's my calendar subscription URL?"
(`calendar_subscription` tool) or build it yourself:
`webcal://<host>/calendar/<CALENDAR_TOKEN>.ics`. On Mac: Calendar › File › New
Calendar Subscription and set auto-refresh to every 5 minutes or hourly. On iPhone, go to Settings ›
Apps › Calendar › Calendar Accounts › Add Account › Other › Add Subscribed Calendar. The calendar is read-only, works on any
device, and covers the last 60 days plus everything upcoming. Updates arrive when Apple
refreshes the subscription, not instantly.

**HoodaRun (iPhone).** Add `ios/HoodaRun/MCPCalendarSync.swift` to the app
target and `NSCalendarsFullAccessUsageDescription` to Info.plist, then turn on
**Claude plans › Add to Apple Calendar**. Each sync writes timed events into a
"HoodaRun" calendar (on iCloud when available) that match the workouts on the
Watch: changed workouts update in place, dropped ones are removed, past events
stay as history.

Events show the steps and paces in their notes, e.g.

```
Plan: Aramco Half
Warm-up: 15 min @ HR zone 2
3×
  Work: 2 mi @ 6:15–6:25/mi
  Recover: 2 min @ HR zone 2
Cool-down: 10 min @ HR zone 2
```
