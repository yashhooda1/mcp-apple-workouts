# HoodaRun Web — athlete training studio

HoodaRun includes a self-hosted, responsive browser app served by the **same
Python process as the MCP server**. It is not a static mockup: its protected
API calls the existing WorkoutKit JSON compiler, marathon and road-plan
generators, strength presets, grocery planner, race catalog, and workout feed.

## Start on your Mac

```bash
git clone https://github.com/yashhooda1/mcp-apple-workouts.git
cd mcp-apple-workouts
python3 -m pip install -e ".[dev]"
export TRANSPORT=streamable-http
export HOST=127.0.0.1
export PORT=8000
apple-workouts-mcp
```

Open **http://127.0.0.1:8000/app** or the root `/` in a browser.

For a localhost-only development server, you may leave `MCP_AUTH_TOKEN`
unset. For a persistent server on your home network or any Internet-facing
host, **set `MCP_AUTH_TOKEN`** and terminate TLS using your hosting platform:

```bash
export TRANSPORT=streamable-http
export HOST=0.0.0.0
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
export WORKOUT_FEED_DIR=/data/feed
apple-workouts-mcp
```

Configure HTTPS, persistent storage, and the iPhone companion's Remote server
settings in your host. Connect in the website by pasting the server's bearer
token into **Connect server**. The token stays in the open browser tab's memory
only; it is not stored in localStorage, cookies, or the web page's source.
Closing or refreshing the page requires reconnection. Avoid using this on
shared computers. The public static HTML contains no personal data.

### Routes

| Route | Authentication | Purpose |
| --- | --- | --- |
| `GET /`, `GET /app` | Public | Browser training studio |
| `GET /app/app.css`, `GET /app/app.js` | Public | Packaged assets |
| `POST /api/web` | Bearer token when configured | Read, preview, publish |
| `POST /mcp` | Bearer token when configured | Standard MCP clients |
| `GET /feed.json` | Bearer token when configured | iPhone HoodaRun sync |
| `POST /status` | Bearer token when configured | iPhone schedule status |

A public page **does not** expose your feed, training history, or data; the
private JSON API is protected by the existing bearer middleware and also
rejects cross-origin browser requests. Browser JavaScript is self-contained,
without CDN assets or tracking scripts.

### Features

- **Dashboard:** Published workouts, upcoming sessions, last app sync, and
  which feed entries were confirmed on the Watch.
- **Training plans:** Adult sub-5 mile, sub-17 5K, sub-1:20 half, or sub-3
  marathon planning using current mileage and optional benchmark; preview and
  inspect the schedule before publishing.
- **Workout builder:** Configurable repetitions, 400m/1km/mile work segments,
  per-mile pace targets, standing rest versus jog recovery, warmup/cooldown,
  and indoor or outdoor location.
- **Strength studio:** Preview and publish Strength A (posterior chain),
  Strength B (hips/core) or short mobility sessions.
- **Nutrition:** Seven-day vegetarian/vegan meals and a categorized,
  checkable grocery list; optional clipboard export.
- **Texas races:** Curated Austin, Dallas/Fort Worth and Houston road events
  with official links, dates and filters. Not live registration inventory.
- **Watch schedule:** Shows whether HoodaRun's **iPhone** sync reported that
  a published feed entry has reached WorkoutKit.

### How publishing works

The web API has separate `preview` and `publish` actions. A preview
compiles the training plan without writing any files and returns a short-lived
signed token. Publishing requires that exact unmodified preview (15-minute
expiry), plus an explicit confirmation in the browser. The feed update uses
the same `FeedStore` and `push_plan` implementations as MCP tools.

`published` means the file was updated, **not** that it reached your watch.
The actual watch scheduling step still requires a paired iPhone running the
HoodaRun app. WorkoutKit's schedule-count limit still applies.

### Limitations and security

- Apple Health/WorkoutKit APIs are **not directly accessible** from a generic
  browser, nor from your laptop Python process. The iPhone companion app is
  required for Apple Watch scheduling and live watch-health access.
- No Apple Sign In, CloudKit web-user account, multi-user role isolation, or
  live Strava history is implemented by this web app. This is a
  **single-user, self-hosted dashboard** for the existing single feed.
- Do not share a single server instance and token among unrelated athletes;
  they would all see and modify the same workout feed. For a public,
  multi-account service, add per-user OAuth, isolated stores, user-specific
  HealthKit permissions, and comprehensive authorization/auditing first.
- Do not host without HTTPS and authentication. Frontend pages are public,
  but the workout-management API must remain private.
- Website frontend and API work on phones and desktop; actual Ultra 4/watchOS
  27 compatibility needs a signed, tested HoodaRun iPhone integration.

## Developer checks

```bash
pytest -q
ruff check .
node --check src/apple_workouts_mcp/web_assets/app.js
```

The Python test suite covers the authenticated routes, preview-to-publish
flow, target validation, standing/jog recoveries, packaged frontend, and
race/nutrition read behavior.
