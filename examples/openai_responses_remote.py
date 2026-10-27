"""Remote OpenAI Responses API MCP example.

This is for a reachable HTTPS Streamable HTTP MCP endpoint, not localhost.
Uses an app-provided Authorization bearer token; approvals are not bypassed.
Install: pip install openai
"""
import os

from openai import OpenAI


def main() -> None:
    url = os.environ.get("WORKOUTS_MCP_URL", "")
    token = os.environ.get("WORKOUTS_MCP_TOKEN", "")
    if not url.startswith("https://") or not token:
        raise SystemExit("Set WORKOUTS_MCP_URL=https://.../mcp and WORKOUTS_MCP_TOKEN")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY")

    response = OpenAI().responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-6"),
        input=os.getenv(
            "RUNNER_PROMPT", "List verified Austin Texas 5K races after October 2026."
        ),
        tools=[{
            "type": "mcp",
            "server_label": "hoodarun",
            "server_url": url,
            "authorization": token,
            "require_approval": "always",
        }],
    )
    # If the server wants approval, inspect mcp_approval_request output and
    # approve explicitly in your app's own trusted UI before resuming.
    print(response.output_text or response.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
