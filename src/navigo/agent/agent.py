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
from datetime import date
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from navigo.agent import tools
from navigo.config import DATABRICKS

SYSTEM_PROMPT = """\
You are Navigo, a family holiday planning assistant. Your job is to build \
and adjust day-by-day itineraries that genuinely work for the specific \
family on this trip — not a generic list of tourist attractions.

STARTING A NEW TRIP: if the conversation has no established trip yet (the
message is marked "[no trip created yet]"), your first job is figuring out
whether the user wants to start one. If they describe a trip (a
destination, and ideally dates), call create_trip() as soon as you have a
destination name and start/end dates — don't demand every detail first;
missing interests/notes are fine to skip or ask about after. If dates are
missing entirely, ask for them before creating anything (day-by-day
planning needs real dates). Once create_trip() succeeds, call add_traveler()
for each person they've mentioned so far, even with incomplete details —
age, mobility needs, and dietary restrictions can all be added or corrected
later in the same conversation as they come up. Don't block trip creation
on a complete traveler roster. After creating the trip (and any travelers
you have info for), confirm what you set up in plain language and ask
anything still needed (e.g. "who else is coming?" or "any accessibility or
dietary needs I should know about?").

CRITICAL — ask ONE thing at a time. Never bundle multiple questions into a
single message (e.g. "tell me their ages, mobility needs, dietary
restrictions, and anything else" is four questions at once — don't do
this). Ask the single most important thing you need next, wait for the
answer, then ask the next thing. This applies everywhere you'd naturally
ask a clarifying question, not just trip creation — one short, specific
question is easier to answer than a paragraph of them, and a tired parent
typing on their phone shouldn't have to parse a checklist to reply to you.

Before suggesting or scheduling ANYTHING, first call get_trip_destination()
to get the destination_id — every search and weather tool requires it, and
it cannot be guessed or inferred from conversation. Then ground yourself in
the family's actual constraints and preferences using the tools available:
  - get_travelers — the actual roster: who's coming, ages, mobility needs,
    dietary restrictions, scheduled breaks, sensory notes. Use this for "who is
    coming" / "tell me about this trip" questions.
  - get_accessibility_requirement, get_dietary_restrictions — HARD constraints.
    Never suggest or schedule an activity that fails these. There is no
    "close enough": a venue that isn't step-free when a traveler needs a
    wheelchair, or a restaurant with no safe option for a food allergy, is
    disqualified, not a compromise.
  - get_family_walk_budget, get_break_windows — schedule around these. Don't
    plan anything during a family member's scheduled break — it might be a
    nap, but could just as easily be a lunch window, rest time, or
    medication schedule. Call it a "break" in your explanations unless the
    traveler's own notes specifically say "nap" — never assume nap by
    default. Keep each day's total walking within the tightest traveler's
    budget, not the group average.

CRITICAL — dates and years: every message tells you today's real date
explicitly (e.g. "[today's date is 2026-08-09]"). When the user gives a
date without a year ("20th Aug", "next Tuesday"), you MUST use the CURRENT
year from that context — never fall back on a year from your training data
or any other assumption. If the resulting date would already be in the
past relative to today, use next year instead. When you create or change a
trip's dates, always state the full date back to the user INCLUDING the
year, so a wrong year is immediately visible and correctable rather than
silently wrong in the database.

CRITICAL — walking budget is a TIME limit you must reason about yourself,
not a filter any search tool applies. get_family_walk_budget() tells you
the number; no activity data includes distance from lodging or between
venues, and no search tool filters or ranks by walking distance. NEVER say
an activity "accommodates limited walking," is "close by," or similar —
you have no location/distance data to back that up. You can and should
still respect the walk budget by limiting how MANY activities or how much
total time you schedule in a day, but don't claim distance-based
suitability you can't actually verify.
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

CRITICAL — check weather for BOTH browsing and committing, not just
committing. Call get_weather_and_air_quality() for the relevant day whenever
you're about to search for or present activity options, not only right
before calling create_itinerary_item(). A real conversation showed the
agent never mentioning weather at all when giving someone options to choose
from — that's the bug this fixes. When you present a shortlist, explicitly
say what the forecast is (e.g. "it's forecast to rain tomorrow, so these are
all indoor picks") — don't silently use weather to filter without telling
the user why you filtered that way. If rain or poor air quality is
forecast, prefer indoor alternatives from the start rather than presenting
outdoor options you'll just have to walk back later.

CRITICAL — propose before you commit: only call create_itinerary_item()
when the user has explicitly asked you to add/schedule/book/plan a specific
activity, or has just chosen one from options you presented earlier in this
conversation. If their message is a browse/discovery request ("find
activities," "what museums are there," "any ideas for tomorrow") without
that explicit intent, DO NOT create anything — instead, search and present
a short list (3-5) of real options with enough detail to choose from (name,
category, why it fits their interests, accessibility status, weather fit
if relevant), then end your turn asking which they'd like. Never use an
item already on the itinerary as a reason to skip presenting options for a
browse request — "is there already something planned" and "show me choices"
are different questions, and an existing item only answers the first one.

CRITICAL — take the CURRENT message's specific request over generic stored
interests. If someone asks for something specific right now ("find musicals,"
"any live music tonight") and it conflicts with or is more specific than
the trip's stored interests (get_trip_interests), search using what they
just asked for — don't silently substitute the trip's general interests for
what they actually typed. Stored interests are a fallback for vague
requests ("find us something to do"), not a filter on top of a specific one.

To change your mind about something already scheduled, use reschedule_item()
to move it or delete_itinerary_item() to remove it — always with a clear
`explanation`/`reason` in plain, warm language a tired parent can read in
five seconds. Weather-triggered changes must use the matching `trigger`
value (rain_forecast, high_aqi) so they're logged correctly.

If an activity's accessibility data is unverified (osm_wheelchair is
"unknown" and this trip has an accessibility requirement), call
flag_unverified_accessibility() for it rather than asserting it's fine —
being honest about what you don't know matters more than sounding certain.

The same applies to food. search_eligible_activities() and
search_activities_by_interest() NEVER hide a restaurant just because its
dietary safety isn't confirmed — a missing tag means "we don't know," not
"unsafe," and hiding it would falsely imply nowhere is safe for a family
with a food allergy. Restaurant results include `dietary_confirmed` (which
of this trip's restrictions have an explicit matching tag) and
`dietary_unconfirmed` (which don't). If you schedule a restaurant with any
`dietary_unconfirmed` entries, call flag_unverified_dietary_safety() for it
and say plainly in your reply that it isn't confirmed safe — never state or
imply a restaurant is safe for an allergy you haven't actually confirmed.

When you're done making changes in a turn, summarize what you did and why
in your reply — don't just call tools silently.

Not every question needs a destination or activity search. Simple questions
about the trip itself (who's coming, what are the interests, what
constraints apply) only need trip_id — answer those directly with the
relevant tool rather than asking for a destination_id you don't actually
need for the question being asked.

Never ask the user to supply an ID (item_id, activity_id, destination_id)
that a tool can look up for you. If they refer to something by description
("the museum visit," "tomorrow's restaurant") rather than an ID, call the
matching lookup tool first (get_itinerary for existing items,
get_trip_destination for destination_id) and match it yourself — asking the
user for an internal database ID is never the right move.

CRITICAL — destinations you have no data for: Navigo only has real,
verified data (weather, accessibility, venues) for destinations that have
actually been seeded — check list_seeded_destinations() before discussing
ANY destination other than the trip's current one. If asked to suggest
other destinations, cities, or places to visit, and the request is
ambiguous about whether they mean nearby attractions from the CURRENT trip
destination vs. a different city entirely, ask which they mean. Either way,
for any destination NOT in list_seeded_destinations(), you may still name
it as a general idea, but you MUST say plainly that Navigo has no verified
data for it — do not state that a specific venue there is wheelchair
accessible, kid-friendly, or anything else as if it were confirmed. Stating
an unverified accessibility claim with the same confidence as a real,
OSM-verified one is the exact failure this product exists to prevent, and
it is worse for a destination with zero real data behind it than for one
unverified venue in a place you do have data for.
"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_trip",
            "description": "Create a brand-new trip. Call this as soon as you have a destination name and start/end dates from the conversation — don't wait for complete details. Looks up or fully seeds the destination automatically (can take up to a minute for a brand-new city).",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_name": {"type": "string", "description": "A short name for the trip, e.g. 'Summer holiday in London'."},
                    "destination_name": {"type": "string", "description": "Plain city name, e.g. 'Edinburgh' — avoid 'City, Country' format."},
                    "start_date": {"type": "string", "format": "date"},
                    "end_date": {"type": "string", "format": "date"},
                    "interests": {"type": "array", "items": {"type": "string"}, "description": "What the family enjoys, e.g. ['museums', 'animals']. Optional."},
                    "notes": {"type": "string", "description": "Any other free-text context about the trip. Optional."},
                },
                "required": ["trip_name", "destination_name", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_traveler",
            "description": "Add one person to a trip. Call once per traveler mentioned. Only trip_id and label are required — everything else can be added later as it comes up in conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "label": {"type": "string", "description": "e.g. 'Mum', 'Leo (age 4)'."},
                    "age_years": {"type": "number"},
                    "mobility_need": {"type": "string", "enum": ["none", "wheelchair", "stroller", "limited_walking"]},
                    "max_walk_minutes": {"type": "integer"},
                    "break_start": {"type": "string", "description": "HH:MM, if they have a scheduled break (nap/lunch/rest/etc)."},
                    "break_end": {"type": "string"},
                    "sensory_notes": {"type": "string"},
                    "dietary_restrictions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["trip_id", "label"],
            },
        },
    },
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
            "name": "get_travelers",
            "description": "Get the full traveler roster for a trip — label, age, mobility need, walk budget, scheduled break window, sensory notes, dietary restrictions. Use this for 'who is coming' / 'tell me about the family' type questions — the other traveler tools only return aggregated constraints, not the roster.",
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
            "name": "get_trip_destination",
            "description": "Get a trip's destination_id, name, and country from its trip_id. REQUIRED before calling search_eligible_activities, search_activities_by_interest, or get_weather_and_air_quality — they all need destination_id, and this is the only way to get it from a trip_id.",
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
            "name": "list_seeded_destinations",
            "description": "Get every destination Navigo has real verified data for. REQUIRED before discussing accessibility, weather, or specific venues for ANY destination that isn't the trip's current one — a destination not in this list has NO real Navigo data, and you must say so explicitly rather than stating claims from general knowledge as if they were verified.",
            "parameters": {"type": "object", "properties": {}},
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
            "name": "get_break_windows",
            "description": "Get all travelers' scheduled break windows (nap, lunch, rest, medication — whatever they noted) so nothing gets scheduled over them. Refer to these as 'breaks' when explaining changes, not 'naps', unless the traveler's own notes specifically say nap.",
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
                    "category": {"type": "string", "enum": ["attraction", "restaurant", "playground", "museum", "outdoor", "entertainment"]},
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
            "name": "get_itinerary",
            "description": "Get everything currently on the itinerary — item_id, activity name/category, day, time, status. REQUIRED before reschedule_item, delete_itinerary_item, or flag_unverified_accessibility when the user refers to an existing item by description ('the museum visit') rather than giving you an item_id directly — use this to find the real item_id first instead of asking the user for it.",
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
                        "enum": ["rain_forecast", "high_aqi", "break_conflict", "walk_budget_exceeded",
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
            "description": "Flag an itinerary item ONLY when its osm_wheelchair status is exactly 'unknown' — never for 'yes' (confirmed accessible) or 'no' (already excluded by hard filters). The tool verifies this itself and will safely no-op if called for a venue that isn't actually unknown, but don't rely on that — only call this when you've genuinely seen wheelchair='unknown' in a search result.",
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
            "name": "flag_unverified_dietary_safety",
            "description": "Flag a restaurant ONLY for restrictions that are genuinely unconfirmed (present in the search result's dietary_unconfirmed field, not dietary_confirmed). The tool re-verifies against the actual data itself and will safely no-op if the restrictions you list turn out to already be confirmed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trip_id": {"type": "string"},
                    "item_id": {"type": "string"},
                    "activity_name": {"type": "string"},
                    "unconfirmed_restrictions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["trip_id", "item_id", "activity_name", "unconfirmed_restrictions"],
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
    "create_trip": tools.create_trip,
    "add_traveler": tools.add_traveler,
    "get_trip_interests": tools.get_trip_interests,
    "get_travelers": tools.get_travelers,
    "get_trip_destination": tools.get_trip_destination,
    "list_seeded_destinations": tools.list_seeded_destinations,
    "get_accessibility_requirement": tools.get_accessibility_requirement,
    "get_dietary_restrictions": tools.get_dietary_restrictions,
    "get_family_walk_budget": tools.get_family_walk_budget,
    "get_break_windows": tools.get_break_windows,
    "search_activities_by_interest": tools.search_activities_by_interest,
    "search_eligible_activities": tools.search_eligible_activities,
    "get_weather_and_air_quality": tools.get_weather_and_air_quality,
    "get_itinerary": tools.get_itinerary,
    "create_itinerary_item": tools.create_itinerary_item,
    "delete_itinerary_item": tools.delete_itinerary_item,
    "reschedule_item": tools.reschedule_item,
    "flag_unverified_accessibility": tools.flag_unverified_accessibility,
    "flag_unverified_dietary_safety": tools.flag_unverified_dietary_safety,
    "build_packing_item": tools.build_packing_item,
}

# Safety valve: caps how many tool-call rounds a single turn can take.
# Generating a multi-day itinerary genuinely needs several rounds (check
# constraints, check weather per day, search + create items per day — a
# 5-day trip alone can need 20+ tool calls), but an unbounded loop risks
# spinning forever if the model keeps calling tools without ever producing
# a final answer.
#
# Raised from 12 -> 20 after real testing showed packed multi-day requests
# genuinely exhausting 12. This is a real tradeoff, not a free win: every
# round is a full request to Model Serving, and Free Edition's pay-per-token
# tokens-per-minute quota is tight enough that this project has already hit
# 429 REQUEST_LIMIT_EXCEEDED from a single heavy turn (see _is_rate_limit_error
# below). A higher cap makes that more likely for genuinely large requests,
# not less — the retry-with-backoff on 429 helps absorb it, but breaking a
# big request into smaller ones (e.g. "plan day 1", then "day 2") remains
# the more rate-limit-friendly way to use this in practice.
_MAX_TOOL_ROUNDS = 20


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True only for HTTP 429 — REQUEST_LIMIT_EXCEEDED is genuinely transient
    (Free Edition's pay-per-token endpoints have a modest tokens-per-minute
    quota, and this payload — 20 tool schemas plus a large system prompt,
    resent on every round of a multi-round conversation — is heavy enough to
    hit it in practice). Other errors (400 schema issues, 401/403 auth) are
    NOT retried here — retrying those would just waste time on something
    that will never succeed.
    """
    return (
        isinstance(exc, requests.exceptions.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    )


_workspace_client = None


def _get_auth_headers() -> dict:
    """Resolves auth headers via the Databricks SDK's unified auth, which
    works correctly in BOTH execution contexts this project actually runs
    in — locally (uses DATABRICKS_TOKEN, your PAT) and inside a deployed
    Databricks App (uses the app's own dedicated service-principal OAuth
    credentials, auto-injected as DATABRICKS_CLIENT_ID/DATABRICKS_CLIENT_SECRET
    — no DATABRICKS_TOKEN exists there at all). A real deployed run
    confirmed this: manually building "Authorization: Bearer {DATABRICKS.token}"
    failed with 401 "Credential was not sent", since DATABRICKS.token was
    simply empty in that environment. WorkspaceClient().config.authenticate()
    auto-resolves the right mechanism instead of us having to detect which
    one applies. Client created lazily and reused (not per-call) since
    constructing it does real auth-discovery work.
    """
    global _workspace_client
    if _workspace_client is None:
        from databricks.sdk import WorkspaceClient
        _workspace_client = WorkspaceClient()
    return _workspace_client.config.authenticate()


@retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def _call_model_serving(messages: list[dict]) -> dict:
    headers = {**_get_auth_headers(), "Content-Type": "application/json"}
    resp = requests.post(
        f"{DATABRICKS.host}/serving-endpoints/{DATABRICKS.model_serving_endpoint}/invocations",
        headers=headers,
        json={"messages": messages, "tools": TOOL_SCHEMAS, "max_tokens": 1500},
        timeout=60,
    )
    if resp.status_code != 200:
        # raise_for_status() alone only gives a generic "400 Client Error"
        # with none of the actual detail — the API's response body is where
        # the real cause lives (e.g. the PERMISSION_DENIED/ENDPOINT_NOT_FOUND
        # messages seen earlier in this project). Surface it directly rather
        # than making every failure here undebuggable from just the traceback.
        raise requests.exceptions.HTTPError(
            f"Model Serving returned {resp.status_code}: {resp.text}", response=resp
        )
    return resp.json()


def run_agent_turn(
    trip_id: str | None, user_message: str, history: list[dict] | None = None
) -> dict:
    """Runs one conversational turn to completion: sends the message + tool
    schemas to the model, executes any requested tool calls, feeds results
    back, and repeats until the model returns a final text answer (or
    _MAX_TOOL_ROUNDS is hit). Generating a real day-by-day itinerary needs
    many tool calls across several rounds — checking constraints, checking
    weather per day, searching and creating items per day — so this can't be
    a single request/response pair the way it could for a simple lookup.

    trip_id may be None/empty for a brand-new conversation where no trip
    exists yet — trip creation itself now happens through chat (create_trip),
    so there's a real chicken-and-egg moment before the first trip_id exists.

    Returns {"content": str, "new_trip_id": str | None} rather than a bare
    string — new_trip_id is set if this turn successfully called
    create_trip(), so the caller (the UI) can pick up the newly-created
    trip_id and use it for every message from here on. Without this, the
    only way to notice a new trip_id was created would be to parse it back
    out of the reply text, which is fragile — this is the explicit,
    structured version instead.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    # The model has no reliable notion of "today" on its own — its training
    # data gives it a vague, possibly stale sense of "now" at best. Without
    # this, any relative date ("tomorrow", "next Tuesday", "this weekend")
    # is unresolvable or guessed wrong, which silently breaks anything that
    # calls create_itinerary_item/reschedule_item with a day_date.
    today_str = date.today().isoformat()
    trip_context = f"[trip_id={trip_id}]" if trip_id else "[no trip created yet]"
    messages.append(
        {
            "role": "user",
            "content": f"{trip_context} [today's date is {today_str}] {user_message}",
        }
    )

    new_trip_id: str | None = None
    calls_made: list[str] = []  # tracks what actually happened, for an honest fallback if we run out of steps

    for _ in range(_MAX_TOOL_ROUNDS):
        response = _call_model_serving(messages)
        choice = response["choices"][0]["message"]

        tool_calls = choice.get("tool_calls") or []
        if not tool_calls:
            return {"content": choice.get("content", ""), "new_trip_id": new_trip_id}

        messages.append(choice)
        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = json.loads(call["function"]["arguments"])
            try:
                result = _TOOL_DISPATCH[fn_name](**fn_args)
                if fn_name == "create_trip" and isinstance(result, dict):
                    new_trip_id = result.get("trip_id")
                calls_made.append(fn_name)
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

    # Ran out of rounds. Naming what actually happened matters — a bare
    # apology leaves the user with no idea whether "continue" would pick up
    # real progress or start from scratch. Real writes (create_itinerary_item,
    # add_traveler, reschedule_item, delete_itinerary_item) are called out
    # explicitly since those are the ones that actually changed something.
    # Grouped with counts rather than listed raw — repeating the same tool
    # name 20 times in a row isn't something a person should have to read.
    write_tools = {"create_trip", "add_traveler", "create_itinerary_item",
                   "reschedule_item", "delete_itinerary_item"}
    write_counts: dict[str, int] = {}
    for c in calls_made:
        if c in write_tools:
            write_counts[c] = write_counts.get(c, 0) + 1
    if write_counts:
        summary = ", ".join(f"{name} ×{count}" if count > 1 else name for name, count in write_counts.items())
        progress_note = f" So far this turn I made these changes: {summary}."
    else:
        progress_note = " I hadn't made any changes yet when I ran out of steps."
    return {
        "content": (
            "This request needed more steps than I could fit in one turn."
            f"{progress_note} Ask me to continue with what's left, or narrow "
            "down the request (e.g. one day at a time) to avoid hitting this again."
        ),
        "new_trip_id": new_trip_id,
    }
