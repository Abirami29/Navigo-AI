"""Tests the tool-calling response shape specifically — the harder, less
certain half of agent.py's OpenAI-compatible assumption. test_model_serving_raw.py
confirmed plain content works; this confirms tool_calls do too, before
trusting the full multi-round loop in agent.run_agent_turn().

Sends one real tool schema (get_trip_interests, the simplest one — just
needs a trip_id) and a message that should provoke the model into calling
it, then checks whether the response matches exactly what agent.py's
run_agent_turn() expects:
    choice["tool_calls"][i]["id"]
    choice["tool_calls"][i]["function"]["name"]
    choice["tool_calls"][i]["function"]["arguments"]  (a JSON string)

Prerequisites: DATABRICKS_HOST, DATABRICKS_TOKEN, NAVIGO_MODEL_SERVING_ENDPOINT
set in .env (same as test_model_serving_raw.py).

Run from the repo root:
    python scripts/test_model_serving_tool_calling.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

from navigo.config import DATABRICKS

# Reuse the real schema from agent.py rather than inventing a test-only one —
# if this exact shape doesn't provoke a tool call, that's useful to know too.
GET_TRIP_INTERESTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_trip_interests",
        "description": "Get the trip's stated interests and free-text notes — the family's preferences.",
        "parameters": {
            "type": "object",
            "properties": {"trip_id": {"type": "string"}},
            "required": ["trip_id"],
        },
    },
}


def main() -> None:
    if not DATABRICKS.host or not DATABRICKS.token:
        raise SystemExit("DATABRICKS_HOST and/or DATABRICKS_TOKEN aren't set in .env.")

    url = f"{DATABRICKS.host}/serving-endpoints/{DATABRICKS.model_serving_endpoint}/invocations"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {DATABRICKS.token}",
            "Content-Type": "application/json",
        },
        json={
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use the get_trip_interests tool to look up the interests for "
                        "trip_id 'test-trip-123'. You must call the tool, don't answer directly."
                    ),
                }
            ],
            "tools": [GET_TRIP_INTERESTS_SCHEMA],
            "max_tokens": 300,
        },
        timeout=30,
    )

    print(f"HTTP status: {resp.status_code}\n")
    if resp.status_code != 200:
        print("Non-200 response:")
        print(resp.text)
        return

    data = resp.json()
    print("Full raw response:")
    print(json.dumps(data, indent=2))
    print()

    message = data["choices"][0]["message"]
    tool_calls = message.get("tool_calls")

    if not tool_calls:
        print(
            "No tool_calls in the response — the model answered directly instead of "
            "calling the tool. That's a prompting issue to solve, not necessarily a shape "
            "problem, but it means this run didn't actually exercise the tool-call parsing."
        )
        return

    print(f"Got {len(tool_calls)} tool call(s). Checking agent.py's exact parsing assumptions:\n")
    for call in tool_calls:
        try:
            call_id = call["id"]
            fn_name = call["function"]["name"]
            fn_args_raw = call["function"]["arguments"]
            fn_args = json.loads(fn_args_raw)
            print(f"  id: {call_id}")
            print(f"  function.name: {fn_name}")
            print(f"  function.arguments (parsed): {fn_args}")
            print("  -> agent.py's parsing assumptions HOLD for this response.")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"  MISMATCH: {exc}")
            print("  -> agent.py's _TOOL_DISPATCH / run_agent_turn() parsing will need "
                  "adjusting to match this actual shape — compare against the raw JSON above.")


if __name__ == "__main__":
    main()
