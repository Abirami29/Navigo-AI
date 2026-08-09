"""Lists the Model Serving endpoints that actually exist in your Databricks
workspace, instead of guessing endpoint names one at a time.

Prerequisites: DATABRICKS_HOST and DATABRICKS_TOKEN set in .env (same as
test_model_serving_raw.py).

Run from the repo root:
    python scripts/list_serving_endpoints.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import requests

from navigo.config import DATABRICKS


def main() -> None:
    if not DATABRICKS.host or not DATABRICKS.token:
        raise SystemExit("DATABRICKS_HOST and/or DATABRICKS_TOKEN aren't set in .env.")

    resp = requests.get(
        f"{DATABRICKS.host}/api/2.0/serving-endpoints",
        headers={"Authorization": f"Bearer {DATABRICKS.token}"},
        timeout=30,
    )

    print(f"HTTP status: {resp.status_code}\n")

    if resp.status_code != 200:
        print("Couldn't list endpoints — here's the raw error:")
        print(resp.text)
        return

    endpoints = resp.json().get("endpoints", [])
    if not endpoints:
        print(
            "No endpoints found in this workspace at all. That likely means pay-per-token "
            "Foundation Model endpoints aren't provisioned for this Free Edition workspace "
            "yet — try the Databricks community forum thread on this, or check the AI "
            "Playground in your workspace UI (it may list queryable models even if the "
            "Serving API doesn't return them here)."
        )
        return

    print(f"Found {len(endpoints)} endpoint(s) in this workspace:\n")
    for ep in endpoints:
        name = ep.get("name")
        state = ep.get("state", {}).get("ready", "UNKNOWN")
        task = ep.get("task", "unknown task")
        print(f"  {name}  [state={state}, task={task}]")

    print(
        "\nCopy one of the names above into NAVIGO_MODEL_SERVING_ENDPOINT in .env, "
        "then re-run scripts/test_model_serving_raw.py."
    )


if __name__ == "__main__":
    main()
