# apple-workouts-mcp

[![ci](https://github.com/yashhooda1/mcp-apple-workouts/actions/workflows/ci.yml/badge.svg)](https://github.com/yashhooda1/mcp-apple-workouts/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP
client) **build structured runs, strength sessions and multi-week training
plans and put them on your Apple Watch** — the Apple counterpart to
[mcp-garmin](https://github.com/yashhooda1/mcp-garmin).

```
"Put a 3×2mi tempo at 6:15–6:25 on Tuesday, Strength B on Thursday,
 and a 12-miler with the last 3 at half-marathon pace on Sunday."
   →  built, published, scheduled on the Watch by HoodaRun.
```

## How it reaches the Watch

Apple has no cloud API that writes to a Watch. Structured workouts get there
through **WorkoutKit**, which only runs on the iPhone. So the work is split:

```
Claude ──MCP──▶ apple-workouts-mcp ──writes──▶ workouts.json
                                                   │  (iCloud Drive, or GET /feed.json)
                                                   ▼
                                  HoodaRun (iPhone) ── WorkoutKit ──▶ Apple Watch
                                                   │
                         status.json ◀──writes─────┘  (what actually landed)
```

- The server compiles each workout into JSON that maps **1:1 onto WorkoutKit**
  (`CustomWorkout` → warmup / `IntervalBlock`s / cooldown, `HeartRateZoneAlert`,
  `SpeedRangeAlert`, time / distance / open goals).
- **HoodaRun** reads the feed, schedules new or changed workouts with
  `WorkoutScheduler`, removes ones that left the feed, and writes `status.json`
  back — so `list_scheduled` can tell you what's really on your wrist.
- Completed runs reach Strava through Strava's own Apple Health sync; the server
  reads them back (read-only, your own Strava API app) for fitness assessment.

Nothing third-party sits in the middle: iCloud Drive for local use, or your own
HTTP host.

## Quick start (Mac + iCloud Drive)

```bash
git clone https://github.com/yashhooda1/mcp-apple-workouts
cd mcp-apple-workouts
pip install -e ".[dev]"
pytest -q                     # offline: compiles workouts + feed round-trips
```

Add to Claude Desktop's MCP config (see `examples/claude_desktop_config.json`):

```json
{ "mcpServers": { "apple-workouts": { "command": "apple-workouts-mcp" } } }
```

On a Mac the feed defaults to **iCloud Drive › HoodaRun › MCP**
(`WORKOUT_FEED_DIR` overrides). In HoodaRun: **Settings › Claude plans ›
Choose feed folder…** and pick that folder once. HoodaRun syncs on launch and
from **Sync now**.

### Optional: Strava history

1. Create an API app at https://www.strava.com/settings/api (callback domain
   `localhost`) and set `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET`.
2. Run `apple-workouts-strava-auth` once. The rotating refresh token is saved to
   `STRAVA_TOKENS` (default `~/.hooda-workouts/strava_tokens.json`, mode 600).

Without Strava, pass `current_weekly_miles` to the plan tools.

## Remote (claude.ai custom connector)

Deploy with the `Dockerfile` / `railway.toml`, attach a volume at `/data`, set
`MCP_AUTH_TOKEN` (`openssl rand -hex 32`). The same bearer token gates all three
routes:

| Route | Who | |
|---|---|---|
| `POST /mcp` | Claude | MCP streamable HTTP |
| `GET /feed.json` | HoodaRun | the workout feed |
| `POST /status` | HoodaRun | what's scheduled on the Watch |

In HoodaRun use **Remote server (HTTP)** with your `https://` host and token
(stored in the Keychain).

## Tools

| Tool | Access | Description |
|---|---|---|
| `list_activities` | read | Recent Strava activities |
| `assess_fitness` | read | Summarise recent training; size the gap to a goal time |
| `list_scheduled` | read | Feed workouts, each flagged `on_watch` from HoodaRun's report |
| `sync_status` | read | Last HoodaRun sync: scheduled, pending, errors |
| `preview_workout` | read | The exact WorkoutKit-shaped JSON for a spec |
| `list_marathons` / `pick_random_marathon` | read | Race catalogue |
| `create_workout` | write | Structured run on a date (optional time, indoor/outdoor) |
| `create_strength_workout` | write | Guided strength/core session on a date |
| `unschedule_workout` | write | Remove one workout by id |
| `clear_scheduled` | write | Remove a date range (dry-run first) |
| `create_training_plan` | plan | Publish a plan you wrote — runs and lifts, by date |
| `create_marathon_plan` | plan | Any race, any goal — built from your actual fitness |
| `create_hs_track_plan` | plan | High-school 800 / 1600 / 3200 / 5K blocks |

Every generated plan schedules **two strength/core sessions a week** and returns
per-week nutrition, hydration and recovery guidance.

## Workout spec

Same schema as mcp-garmin, so prompts and plans port unchanged:

```json
{"name": "4x1mi threshold", "location": "outdoor", "steps": [
  {"kind": "warmup", "duration": "15:00", "target": "hr:2"},
  {"repeat": 4, "steps": [
    {"kind": "run", "duration": "1mi", "target": "pace:6:35-6:55"},
    {"kind": "recovery", "duration": "2:30", "target": "hr:2"}]},
  {"kind": "cooldown", "duration": "10:00", "target": "hr:2"}]}
```

- **Durations:** `"15:00"`, `"90s"`, `"1mi"`, `"400m"`, `"1km"`, `"open"` (lap button)
- **Targets:** `"hr:2"` → `HeartRateZoneAlert`, `"pace:M:SS-M:SS"` (per mile) →
  `SpeedRangeAlert`, or none
- **Strength:** `{"name": …, "exercises": [{"category": "PLANK", "exercise":
  "SIDE_PLANK", "sets": 3, "hold": "30s", "rest_s": 30}]}` — rep sets are open
  steps you end with the Action button; holds and rest are timed

## Things WorkoutKit decides, not us

- **Scheduled-workout cap.** WorkoutKit only holds
  `WorkoutScheduler.maxAllowedScheduledWorkoutCount` workouts at once (shared
  with anything HoodaRun schedules itself). HoodaRun fills free slots with the
  soonest sessions and reports the rest as `pending`; each sync rolls forward,
  so open the app every few days during a long block.
- **One warmup and one cooldown.** A leading `warmup` and trailing `cooldown`
  become WorkoutKit's; everything else becomes interval blocks, in order.
- **Strength** is scheduled as Traditional Strength Training, falling back to
  Functional Strength if the Watch doesn't support it as a custom workout.

## Building on the web (yashhooda.ai + CloudKit)

The **HoodaRun** section on yashhooda.ai is a workout builder that saves to the
visitor's own **private CloudKit database** after they sign in with their Apple
ID (CloudKit JS). HoodaRun reads those records alongside the MCP feed, schedules
them with the same code path, and writes `scheduledRev` back so the site shows
**On Watch**. Web workouts use the identical `hooda.workouts.custom/v1` JSON.

One-time setup in [CloudKit Console](https://icloud.developer.apple.com):

1. Container `iCloud.ai.yashhooda.HoodaRun` (or change `containerID` in
   `CloudKitPlanStore.swift` and `CK.container` in the site section to match
   HoodaRun's real container). Add it under HoodaRun › Signing & Capabilities ›
   iCloud › CloudKit.
2. Record type `PlannedWorkout` with String fields `date`, `time`, `name`,
   `sport`, `workoutJSON`, `rev`, `source`, `scheduledRev`, and Date/Time
   `syncedAt`.
3. Indexes: `date` Queryable + Sortable, `recordName` Queryable.
4. Deploy the schema to Production.
5. API Access › new **web API token**, sign-in callback/allowed origin
   `https://yashhooda.ai`. Paste it into `CK.apiToken` in the site section;
   until then the builder runs in preview mode (build + copy JSON, no sign-in).

## HoodaRun integration

Add the three files in `ios/HoodaRun/` to the HoodaRun target (iOS 18+ /
watchOS 11+, WorkoutKit, CloudKit):

- `MCPPlanSync.swift` — feed models, JSON → `CustomWorkout`, the sync actor
- `CloudKitPlanStore.swift` — reads/marks workouts saved from yashhooda.ai
- `MCPSyncSettingsView.swift` — source picker + **Sync now**

Then two insertions in the app:

```swift
// In HoodaRun's settings list:
NavigationLink("Claude plans") { MCPSyncSettingsView() }

// In the App struct, sync whenever HoodaRun comes to the foreground:
@Environment(\.scenePhase) private var scenePhase
// ...on the root WindowGroup content:
.onChange(of: scenePhase) { _, phase in
    if phase == .active { Task { try? await MCPPlanSync.shared.sync() } }
}
```

The sync only ever removes workouts it scheduled itself (tracked by id), so
workouts made in HoodaRun's own builder are never touched.

## License

MIT
