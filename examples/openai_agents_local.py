"""Local OpenAI Agents SDK example using this repo's MCP stdio transport.

Install: pip install -e . && pip install openai-agents
Set OPENAI_API_KEY before running. Read-only tools are exposed by default.
"""
import asyncio
import os

from agents import Agent, Runner
from agents.mcp import MCPServerStdio, create_static_tool_filter


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY in your shell (not in source files).")
    # Starting a local process avoids exposing workout data to a public endpoint.
    async with MCPServerStdio(
        name="HoodaRun Apple workouts",
        params={"command": "apple-workouts-mcp", "args": []},
        tool_filter=create_static_tool_filter(allowed_tool_names=[
            "list_texas_races", "build_meal_grocery_plan", "list_marathons",
            "preview_workout", "sync_status", "list_scheduled",
        ]),
        client_session_timeout_seconds=30,
    ) as server:
        agent = Agent(
            name="Runner assistant",
            instructions=(
                "Use MCP for concrete races and meal suggestions. Do not claim "
                "you added a workout to Apple Watch without sync_status."
            ),
            mcp_servers=[server],
        )
        prompt = os.getenv(
            "RUNNER_PROMPT", "Find Austin 5K races and make an easy vegan grocery plan."
        )
        result = await Runner.run(agent, prompt)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
