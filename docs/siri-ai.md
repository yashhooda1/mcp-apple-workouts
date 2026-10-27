# Siri AI, Apple Intelligence and Shortcuts integration

HoodaRun can expose workout actions to **Siri AI on iOS 27** using Apple's
**App Intents / App Shortcuts** framework. Siri has **no public native,
generic MCP client connector** where you simply enter an MCP server URL.
App Intents is Apple's supported way to make actions discoverable by
Siri, Siri AI, Spotlight and Shortcuts.

In this integration, **Siri itself is not an MCP client**. The HoodaRun
iPhone app interprets a typed App Intent and calls existing local
iCloud/CloudKit sources or a small authenticated HTTP bridge to the same
Python service powering the MCP tools. This allows Siri access to selected
HoodaRun features without exposing arbitrary MCP execution to Siri.

## What's included

| App Shortcut | How it works | Read/write |
| --- | --- | --- |
| Next HoodaRun Workout | Reads the next planned workout from the existing local feed or CloudKit | Read |
| Sync HoodaRun to Apple Watch | Calls MCPPlanSync on the paired iPhone; confirms scheduled/pending/errors | Watch sync |
| Add HoodaRun Easy Run | Asks for mileage and optional date, previews and requests confirmation before writing | Write (remote only) |
| Add HoodaRun Intervals | Repeats, interval length, pace range, jogging/standing rest, recovery time and date | Write (remote only) |
| Find Texas Races | Query the repo's curated Austin, Dallas/Fort Worth, Houston race calendar | Read (remote only) |
| HoodaRun Meal Ideas | Simple vegan/lacto-vegetarian/vegetarian meal suggestions | Read (remote only) |

The remote bridge is deliberately restricted to these operations, not an
arbitrary remote AI prompt or tool executor. The original MCP endpoint
remains available to ChatGPT/OpenAI clients under its existing permissions.

## Installation — iPhone app

1. Add **`ios/HoodaRun/HoodaRunSiriIntents.swift`** to the
   actual **HoodaRun iPhone app target** in Xcode. Add it to any intent
   extension target only if that extension supports the same shared
   settings, Keychain, and network entitlement; don't duplicate shortcut
   providers. Keep `MCPPlanSync.swift` and `CloudKitPlanStore.swift`
   in the app target.
2. Use a recent Xcode supporting iOS 27 SDK; add the project's
   Siri/Shortcuts capabilities as appropriate. Confirm the app's actual
   target name is **HoodaRun** so the phrases using
   `\(.applicationName)` can be spoken naturally.
3. App Shortcuts are normally automatically discovered. For proactive
   shortcut parameter refresh on app launch, call:
   `HoodaRunSiriShortcuts.updateAppShortcutParameters()` from your
   app's startup path after initialization.
4. In HoodaRun Settings, choose iCloud Drive or CloudKit for local
   **Next Workout** and **Sync**. To enable voice-created workouts
   and race/meal queries, choose **Remote server (HTTP)** and configure
   your **HTTPS origin** and existing `MCP_AUTH_TOKEN`. HoodaRun
   already saves that token in the iPhone's Keychain. Avoid putting
   tokens in Siri phrases or Shortcuts input fields.
5. In iPhone **Shortcuts → App Shortcuts → HoodaRun**, verify each
   action is discovered. Invoke through Siri using a phrase below,
   or assemble a custom Shortcut with the corresponding action.
6. Test on a physical paired iPhone and Apple Watch. If you want to
   invoke the Shortcut from the Watch, enable it for the Watch in
   Shortcuts and confirm its required actions run on your watch/iPhone
   configuration. A **watchOS app target is not included in this repo**:
   Siri voice actions on the Watch are not hardware-verified yet.

## Try voice commands

- **“Siri, what's my next workout in HoodaRun?”** (works with a local feed)
- **“Siri, sync my HoodaRun workouts.”** (invokes iPhone WorkoutKit sync)
- **“Siri, schedule an easy run in HoodaRun.”** (Siri asks for mileage)
- **“Siri, add interval training in HoodaRun.”** (repeat, distance,
  min/mile pace window, recovery seconds, stand/jog, optional date)
- **“Siri, find Texas races in HoodaRun.”** (choose Austin/DFW/Houston)
- **“Siri, get meal ideas from HoodaRun.”**

Exact natural-language triggering depends on iOS Siri AI and whether the
app's shortcuts have been indexed. More semantically flexible requests
may require App Schema adoption; the first version uses explicit
App Shortcut phrases for predictable discovery.

## Private server API

When the Python server is started with
`TRANSPORT=streamable-http` and a configured `MCP_AUTH_TOKEN`,
the extra route `POST /api/siri` is available at the same host.

Its allowed actions are:
`next_workout`, `texas_races`, `meal_ideas`,
`preview_run`, `publish_run`.

The app uses only `https://your-host/api/siri` for remote actions;
an iCloud-only setup supports next-workout and sync without the API.
The remote voice endpoint is **behind the same constant-time bearer
gate** as `/mcp`, `/feed.json`, and `/status`.

For a run publication, the flow is:

1. iPhone sends strictly validated session fields to `preview_run`.
2. Server validates date (today through 30 days away), mileage,
   repeat count, pace targets, and recoveries. It builds WorkoutKit
   JSON in memory without touching the feed. It returns a short-lived
   **signed preview token**.
3. App Intents calls **`requestConfirmation()`** on the iPhone,
   waiting for actual user approval.
4. After approval, the app calls `publish_run` with the same parameters
   and token; any change or expiration invalidates it.
5. Server writes to the same **FeedStore** used by the MCP tools.
   The user must sync HoodaRun on iPhone before Watch availability
   is confirmed.

Security and operational constraints:

- Do **not** bypass the token middleware. For public hosting, use TLS,
  persistent storage, and an environment-managed secret token.
- Siri actions never receive server credentials in spoken output;
  the app fetches its stored Keychain token at request time.
- Only single-workout writes, not mass replacement or a generic MCP
  proxy, are available to Siri in this iteration.
- If the server process restarts, any outstanding preview tokens are
  invalidated. Preview again.
- Do not mistake a published feed record for a confirmed WorkoutKit
  schedule. The Watch schedule cap and iPhone permission prompts remain.
- This is **not** a compiled Xcode project; the repo supplies Swift
  source integration files. Simulator and physical-device integration,
  signing, entitlements, and watchOS 27/Ultra 4 tests remain necessary.
- Voice requests through Siri/Apple Intelligence are governed by
  Apple's privacy controls. The server only sees the typed data sent
  to the selected private API.

## References

- Apple: https://developer.apple.com/documentation/appintents/apple-intelligence-and-siri-ai
- Apple: https://developer.apple.com/documentation/appintents/app-shortcuts
- Apple: https://developer.apple.com/documentation/appintents/appintent/requestconfirmation()
- Apple WWDC26: https://developer.apple.com/videos/play/wwdc2026/240/
- OpenAI Apple Intelligence: https://help.openai.com/en/articles/10269382-setting-up-chatgpt-with-apple-intelligence
