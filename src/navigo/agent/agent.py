"""Navigo planning agent — orchestrates tool calls against a Databricks-hosted
foundation model via Model Serving.

This is a minimal, framework-agnostic starting point using the OpenAI-compatible
chat completions surface that Databricks Model Serving exposes, so it's easy to
later swap in Mosaic AI Agent Framework / Agent Bricks without rewriting the
tool logic in tools.py.

Reference: https://docs.databricks.com/aws/en/machine-learning/foundation-models/
"""

from __future__ import annotations

import json
from typing import Any

import requests

from navigo.agent import tools
from navigo.config import DATABRICKS

SYSTEM_PROMPT = """\
You are Navigo, a family holiday planning assistant. You plan day-by-day \
itineraries for families that account for children's nap times, walking \
limits, food allergies, and mobility/accessibility needs. You never suggest \
an activity that fails a hard accessibility, age, or dietary requirement. \
When you reschedule anything, you always explain the reason in plain, warm \
language a tired parent can read in five seconds. If an activity's \
accessibility data is unverified, say so rather than asserting it's fine.
"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_eligible_activities",
            "description": "Find activities at a destination that pass hard accessibility/diet/age filters for this trip's travelers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination_id": {"type": "string"},
                    "trip_id": {"type": "string"},
                    "category": {"type": "string", "enum": ["attraction", "restaurant", "playground", "museum", "outdoor"]},
                    "exclude_outdoor": {"type": "boolean"},
                },
                "required": ["destination_id", "trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_and_air_quality",
            "description": "Get hourly weather and air quality for a destination and date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination_id": {"type": "string"},
                    "forecast_date": {"type": "string", "format": "date"},
                },
                "required": ["destination_id", "forecast_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_item",
            "description": "Move an itinerary item to a new day/time and log why.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "new_day_date": {"type": "string", "format": "date"},
                    "new_start_time": {"type": "string"},
                    "new_end_time": {"type": "string"},
                    "trigger": {
                        "type": "string",
                        "enum": ["rain_forecast", "high_aqi", "nap_conflict", "walk_budget_exceeded",
                                 "unverified_accessibility", "user_request"],
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["trip_id", "item_id", "new_day_date", "new_start_time",
                              "new_end_time", "trigger", "explanation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_packing_item",
            "description": "Add a packing list item for a trip, optionally tied to a specific traveler.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "traveler_id": {"type": "string"},
                    "item_name": {"type": "string"},
                    "category": {"type": "string", "enum": ["clothing", "medical", "comfort", "documents", "other"]},
                    "reason": {"type": "string"},
                },
                "required": ["trip_id", "item_name", "category", "reason"],
            },
        },
    },
]

_TOOL_DISPATCH = {
    "search_eligible_activities": tools.search_eligible_activities,
    "get_weather_and_air_quality": tools.get_weather_and_air_quality,
    "reschedule_item": tools.reschedule_item,
    "build_packing_item": tools.build_packing_item,
}


def _call_model_serving(messages: list[dict]) -> dict:
    resp = requests.post(
        f"{DATABRICKS.host}/serving-endpoints/{DATABRICKS.model_serving_endpoint}/invocations",
        headers={"Authorization": f"Bearer {DATABRICKS.token}", "Content-Type": "application/json"},
        json={"messages": messages, "tools": TOOL_SCHEMAS, "max_tokens": 1500},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run_agent_turn(trip_id: str, user_message: str, history: list[dict] | None = None) -> str:
    """Runs one conversational turn: sends the message + tool schemas to the
    model, executes any requested tool calls, and returns the final text reply.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": f"[trip_id={trip_id}] {user_message}"})

    response = _call_model_serving(messages)
    choice = response["choices"][0]["message"]

    # Simple single-round tool-call loop; extend to multi-round if needed.
    tool_calls = choice.get("tool_calls") or []
    if not tool_calls:
        return choice.get("content", "")

    messages.append(choice)
    for call in tool_calls:
        fn_name = call["function"]["name"]
        fn_args = json.loads(call["function"]["arguments"])
        result = _TOOL_DISPATCH[fn_name](**fn_args)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, default=str),
            }
        )

    final_response = _call_model_serving(messages)
    return final_response["choices"][0]["message"].get("content", "")
