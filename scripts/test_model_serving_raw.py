"""Raw Model Serving smoke test.

Calls your Databricks Model Serving endpoint directly with a plain message —
no tools, no multi-round loop, none of agent.py's parsing logic. The point
is to confirm auth actually works and see the REAL response shape before
trusting run_agent_turn()'s assumptions about it (that it's OpenAI-compatible
chat-completions shaped: response["choices"][0]["message"], tool_calls
formatted a certain way, etc.) — those were reasonable guesses based on
Databricks' docs, but untested against a real endpoint until now.

Prerequisites: DATABRICKS_HOST, DATABRICKS_TOKEN, and
NAVIGO_MODEL_SERVING_ENDPOINT set in .env.

Run from the repo root:
    python scripts/test_model_serving_raw.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

from navigo.config import DATABRICKS


def main() -> None:
    if not DATABRICKS.host or not DATABRICKS.token:
        raise SystemExit(
            "DATABRICKS_HOST and/or DATABRICKS_TOKEN aren't set. Fill them in .env first — "
            "see the Serving tab in your workspace for the host, and Settings > Developer > "
            "Access tokens for a PAT with at least the Model Serving Inference scope."
        )

    print(f"Endpoint:  {DATABRICKS.model_serving_endpoint}")
    print(f"Host:      {DATABRICKS.host}")
    print()

    url = f"{DATABRICKS.host}/serving-endpoints/{DATABRICKS.model_serving_endpoint}/invocations"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {DATABRICKS.token}",
            "Content-Type": "application/json",
        },
        json={
            "messages": [{"role": "user", "content": "Say hello in exactly five words."}],
            "max_tokens": 100,
        },
        timeout=30,
    )

    print(f"HTTP status: {resp.status_code}")
    print()

    if resp.status_code != 200:
        print("Non-200 response — this is the actual error to debug from, not a guess:")
        print(resp.text)
        return

    data = resp.json()
    print("Full raw response (this is the real shape — compare against what agent.py assumes):")
    print(json.dumps(data, indent=2))

    print()
    try:
        content = data["choices"][0]["message"]["content"]
        print(f"Parsed content (using agent.py's exact assumption): {content!r}")
        print("This assumption holds — agent.py's parsing should work as written.")
    except (KeyError, IndexError, TypeError) as exc:
        print(f"agent.py's assumed response shape does NOT match: {exc}")
        print("Compare the raw JSON above against navigo/agent/agent.py's _call_model_serving() "
              "and run_agent_turn() — the parsing logic will need adjusting to match.")


if __name__ == "__main__":
    main()
