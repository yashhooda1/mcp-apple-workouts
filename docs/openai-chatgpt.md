# OpenAI and ChatGPT integration

This repository implements standard MCP; it does **not** call an OpenAI model
from the Apple Watch. The Watch path is unchanged:

1. MCP tool generates a structured JSON workout and writes the feed.
2. HoodaRun **on iPhone** reads that feed and schedules workouts using WorkoutKit.
3. The user opens HoodaRun and syncs; `sync_status` / `list_scheduled` show
   what has actually landed on the watch versus what is pending.

There is no direct Apple Watch cloud workout-write API. A running MCP server
alone does not install HoodaRun or provision an Apple developer account.

## Option A: OpenAI Agents SDK (local Mac; simplest for developers)

```bash
pip install -e ".[dev]"
pip install openai-agents
export OPENAI_API_KEY="your-key"  # use a secret manager in production
python examples/openai_agents_local.py
```

The example launches `apple-workouts-mcp` locally over stdio. It exposes
**read-only** tools for race research, groceries and watch sync; publishing
workouts isn't enabled in this demonstration. The actual server exposes
`create_road_race_plan`, `create_marathon_plan` and related write tools
to trusted MCP clients, with `dry_run=True` for generated plans.

Avoid setting `TRANSPORT=streamable-http` when using this stdio example.

## Option B: OpenAI Responses API with hosted remote MCP

```bash
pip install openai
export OPENAI_API_KEY="your-key"
export WORKOUTS_MCP_URL="https://your-host.example/mcp"
export WORKOUTS_MCP_TOKEN="your-existing-MCP_AUTH_TOKEN"
python examples/openai_responses_remote.py
```

Start your server with `TRANSPORT=streamable-http`, a TLS-terminating proxy,
persistent feed storage, and `MCP_AUTH_TOKEN` set. Never check credentials into
Git or send them to a third-party public server. The example passes the token
through the API's `authorization` field and requests tool-call approval.
It **does not automatically approve** any tool call or publish a workout.

OpenAI API tool access is separate from ChatGPT app/connector eligibility.

## Option C: ChatGPT custom MCP server/plugin

ChatGPT is not a general local stdio MCP launcher on the web.

- In an eligible workspace, use **Plugins → Add custom MCP server**, supplying
  the externally reachable HTTPS `/mcp` URL, or select a supported
  **Secure MCP Tunnel** for a local/private service.
- Server authentication must be compatible with the connection flow offered
  by ChatGPT (for example, appropriately configured OAuth, or a supported
  tunnel). **This repo currently supplies only a fixed bearer-token gate** for
  remote HTTP; do not expose it unauthenticated just to make ChatGPT connect.
- Depending on ChatGPT plan/workspace policy, custom MCP write actions may be
  unavailable, require approval, or need admin enablement. Access is not
  guaranteed by installing this Python package.
- If the connection is accepted, refresh its discovered MCP tools after
  deployment. Preview a generated plan first, and deliberately publish it
  only after review. Validate `sync_status` after opening HoodaRun.

References:

- https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp
- https://openai.github.io/openai-agents-python/mcp/

## Suggested runner prompts

- "Preview a 16-week sub-17 5K block with a recent 18:15 5K benchmark, 35 miles
  per week, 5 running days, two strength sessions weekly. Do not publish."
- "Build a sub-5 mile plan with one 400 m session, one threshold session,
  strides and a deload every fourth week."
- "Preview a sub-1:20 half plan. Also build a seven-day lacto-vegetarian easy-cook
  grocery list."
- "Find Austin 5K events and DFW half marathons, using only published dates.
  Include official organizer links."
- "After I review, publish the plan; then check which sessions are actually
  scheduled on my watch."

## Apple Watch compatibility / limitations

The JSON feed format and iPhone WorkoutKit integration are designed for newer
Apple Watches, including **Apple Watch Ultra 4 on watchOS 27**, by using
WorkoutKit rather than assumptions about a particular display or GPS model.
This repo does **not** contain a compiled watchOS 27 app or a hardware test
result. The Swift files under `ios/HoodaRun/` must be added to an iOS app
target and tested on an iPhone paired with the watch.

WorkoutKit may limit how many sessions are scheduled ahead. A feed item marked
`published` is not proof it reached the Watch: trust `sync_status`.

The Watch's heart-rate and training data is not available just by connecting
ChatGPT. Strava history is optional and read-only; no HealthKit extraction,
HRV-driven model or automatic nutrition purchase is implemented here.
