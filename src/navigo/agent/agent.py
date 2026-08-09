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
You are Navigo, a family holiday planning assistant. Your job is to build \
and adjust day-by-day itineraries that genuinely work for the specific \
family on this trip — not a generic list of tourist attractions.

Before suggesting or scheduling ANYTHING, always ground yourself in the \
family's actual constraints and preferences using the tools available:
  - get_accessibility_requirement, get_dietary_restrictions — HARD constraints.
    Never suggest or schedule an activity that fails these. There is no
    "close enough": a venue that isn't step-free when a traveler needs a
    wheelchair, or a restaurant with no safe option for a food allergy, is
    disqualified, not a compromise.
  - get_family_walk_budget, get_nap_windows — schedule around these. Don't
    plan anything during a nap window, and keep each day's total walking
    within the tightest traveler's budget, not the group average.
  - get_trip_interests — the family's stated interests/notes. Use this as
    the search_activities_by_interest() query text (combined with current
    weather conditions) so choices reflect what THIS family wants, not just
    what's nearby.

When picking activities: prefer search_activities_by_interest() over
search_eligible_activities() whenever you have a sense of what the family is
after — it does semantic matching on top of the same hard filters, so it
surfaces things that actually fit their interests rather than just
whatever's in the destination. Fall back to search_eligible_activities() for
plain browsing by category.

Always check get_weather_and_air_quality() for a day before finalizing
outdoor plans for it. If rain or poor air quality is forecast, prefer indoor
alternatives from the start rather than scheduling outdoor activities you'll
just have to reschedule later.

To build the itinerary, call create_itinerary_item() for each activity you
place on the schedule. To change your mind, use reschedule_item() to move
something or delete_itinerary_item() to remove it — always with a clear
`explanation`/`reason` in plain, warm language a tired parent can read in
five seconds. Weather-triggered changes must use the matching `trigger`
value (rain_forecast, high_aqi) so they're logged correctly.

If an activity's accessibility data is unverified (osm_wheelchair is
"unknown" and this trip has an accessibility requirement), call
flag_unverified_accessibility() for it rather than asserting it's fine —
being honest about what you don't know matters more than sounding certain.

When you're done making changes in a turn, summarize what you did and why
in your reply — don't just call tools silently.
"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_trip_interests",
            "description": "Get the trip's stated interests and free-text notes — the family's preferences, used to build a semantic search query.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_accessibility_requirement",
            "description": "Get the strictest mobility need across this trip's travelers (wheelchair/stroller/limited_walking/None). A hard constraint, not a preference.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dietary_restrictions",
            "description": "Get all dietary restrictions across this trip's travelers (e.g. peanut_allergy, vegetarian). A hard constraint for restaurant choices.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_family_walk_budget",
            "description": "Get the most restrictive max-walk-minutes across all travelers for a given day.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "day_date": {"type": "string", "format": "date"},
                },
                "required": ["trip_id", "day_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nap_windows",
            "description": "Get all travelers' nap windows so nothing gets scheduled over them.",
            "parameters": {
                "type": "object",
                "properties": {"trip_id": {"type": "string"}},
                "required": ["trip_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_activities_by_interest",
            "description": "Semantic search for activities matching the family's interests/conditions, restricted to hard-eligible (accessibility/diet) results. Prefer this over search_eligible_activities when you know what the family is after.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "destination_id": {"type": "string"},
                    "query_text": {"type": "string", "description": "Describe what the family wants, e.g. 'outdoor nature activity for young kids, sunny weather' or 'indoor museum, it's raining'."},
                    "top_k": {"type": "integer"},
                    "exclude_outdoor": {"type": "boolean"},
                },
                "required": ["trip_id", "destination_id", "query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_eligible_activities",
            "description": "Find activities at a destination that pass hard accessibility/diet filters, without semantic ranking. Use for plain category browsing.",
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
            "name": "create_itinerary_item",
            "description": "Add an activity to the itinerary at a specific day/time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "activity_id": {"type": "string"},
                    "day_date": {"type": "string", "format": "date"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                },
                "required": ["trip_id", "activity_id", "day_date", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_itinerary_item",
            "description": "Remove an item from the itinerary, with a reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["trip_id", "item_id", "reason"],
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
            "name": "flag_unverified_accessibility",
            "description": "Flag that an itinerary item's accessibility data is unverified OSM data, when this trip has an accessibility requirement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "activity_name": {"type": "string"},
                },
                "required": ["trip_id", "item_id", "activity_name"],
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
    "get_trip_interests": tools.get_trip_interests,
    "get_accessibility_requirement": tools.get_accessibility_requirement,
    "get_dietary_restrictions": tools.get_dietary_restrictions,
    "get_family_walk_budget": tools.get_family_walk_budget,
    "get_nap_windows": tools.get_nap_windows,
    "search_activities_by_interest": tools.search_activities_by_interest,
    "search_eligible_activities": tools.search_eligible_activities,
    "get_weather_and_air_quality": tools.get_weather_and_air_quality,
    "create_itinerary_item": tools.create_itinerary_item,
    "delete_itinerary_item": tools.delete_itinerary_item,
    "reschedule_item": tools.reschedule_item,
    "flag_unverified_accessibility": tools.flag_unverified_accessibility,
    "build_packing_item": tools.build_packing_item,
}

# Safety valve: caps how many tool-call rounds a single turn can take.
# Generating a multi-day itinerary genuinely needs several rounds (check
# constraints, check weather per day, search + create items per day), but an
# unbounded loop risks spinning forever if the model keeps calling tools
# without ever producing a final answer.
_MAX_TOOL_ROUNDS = 12


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
    """Runs one conversational turn to completion: sends the message + tool
    schemas to the model, executes any requested tool calls, feeds results
    back, and repeats until the model returns a final text answer (or
    _MAX_TOOL_ROUNDS is hit). Generating a real day-by-day itinerary needs
    many tool calls across several rounds — checking constraints, checking
    weather per day, searching and creating items per day — so this can't be
    a single request/response pair the way it could for a simple lookup.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": f"[trip_id={trip_id}] {user_message}"})

    for _ in range(_MAX_TOOL_ROUNDS):
        response = _call_model_serving(messages)
        choice = response["choices"][0]["message"]

        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            return choice.get("content", "")

        messages.append(choice)
        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = json.loads(call["function"]["arguments"])
            try:
                result = _TOOL_DISPATCH[fn_name](**fn_args)
            except Exception as exc:
                # Feed the error back to the model as a tool result rather
                # than crashing the whole turn — lets it try a different
                # approach (e.g. a bad activity_id) instead of losing all
                # progress made so far in this turn.
                result = {"error": str(exc)}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str),
                }
            )

    return (
        "I made a lot of changes but ran out of steps before finishing this "
        "turn — could you ask me to continue, or narrow down what's left?"
    )
