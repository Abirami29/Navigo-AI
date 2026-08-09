"""Tool functions exposed to the Navigo planning agent.

Each function here is a candidate for registration as an agent tool (e.g. via
Mosaic AI Agent Framework's @tool decorator or an OpenAI-style tool schema).
They are kept as plain, testable Python functions and wired into the agent
framework in agent.py, so the core logic isn't tied to any one framework.
"""

from __future__ import annotations

import re
from datetime import date, time

from navigo.agent import retrieval
from navigo.db import client as db


def get_family_walk_budget(trip_id: str, day_date: date) -> int | None:
    """Returns the most restrictive max_walk_minutes across all travelers on the trip.

    The itinerary must respect the traveler with the tightest walking budget,
    not the average.
    """
    rows = db.fetch_all(
        "SELECT max_walk_minutes FROM travelers WHERE trip_id = %s AND max_walk_minutes IS NOT NULL",
        (trip_id,),
    )
    if not rows:
        return None
    return min(r["max_walk_minutes"] for r in rows)


def get_nap_windows(trip_id: str) -> list[dict]:
    """Returns all travelers' nap windows so the agent avoids scheduling over them."""
    return db.fetch_all(
        """
        SELECT label, nap_window_start, nap_window_end
        FROM travelers
        WHERE trip_id = %s AND nap_window_start IS NOT NULL
        """,
        (trip_id,),
    )


def get_dietary_restrictions(trip_id: str) -> list[str]:
    rows = db.fetch_all(
        "SELECT dietary_restrictions FROM travelers WHERE trip_id = %s", (trip_id,)
    )
    restrictions: set[str] = set()
    for r in rows:
        restrictions.update(r["dietary_restrictions"] or [])
    return sorted(restrictions)


def get_accessibility_requirement(trip_id: str) -> str | None:
    """Returns the strictest mobility need across travelers, or None if no constraint.

    'wheelchair' is the strictest — if any traveler needs it, every suggested
    activity must have osm_wheelchair in ('yes', 'limited'), never 'no'.
    """
    rows = db.fetch_all(
        "SELECT DISTINCT mobility_need FROM travelers WHERE trip_id = %s", (trip_id,)
    )
    needs = {r["mobility_need"] for r in rows}
    if "wheelchair" in needs:
        return "wheelchair"
    if "stroller" in needs:
        return "stroller"
    if "limited_walking" in needs:
        return "limited_walking"
    return None


def get_trip_interests(trip_id: str) -> dict:
    """Returns the trip's stated interests and free-text notes — the
    "preferences" from the product brief. Used to build the semantic search
    query in search_activities_by_interest(), not as a hard filter.
    """
    row = db.fetch_one("SELECT interests, notes FROM trips WHERE trip_id = %s", (trip_id,))
    if row is None:
        return {"interests": [], "notes": None}
    return {"interests": row["interests"] or [], "notes": row["notes"]}


def _apply_hard_filters(rows: list[dict], trip_id: str) -> list[dict]:
    """The hard-filter half of context engineering, factored out so both
    search_eligible_activities() (pure SQL filtering) and
    search_activities_by_interest() (semantic results filtered afterward)
    apply IDENTICAL rules.

    Accessibility is a genuine hard filter: osm_wheelchair='no' means
    confirmed inaccessible, so those rows are excluded outright. Dietary
    restrictions are NOT filtered the same way, deliberately — see below.
    """
    accessibility_need = get_accessibility_requirement(trip_id)
    dietary_restrictions = get_dietary_restrictions(trip_id)

    if accessibility_need in ("wheelchair", "stroller"):
        rows = [r for r in rows if r["osm_wheelchair"] != "no"]

    if dietary_restrictions:
        # IMPORTANT: this used to exclude any restaurant that had no matching
        # diet:*=yes tag, which meant a trip with any dietary restriction got
        # ZERO restaurant results almost every time — OSM contributors almost
        # never fill in dietary tags, so "no tag" was being treated as "not
        # safe" when it actually just means "we don't know." That's not a
        # near-miss being excluded, it's the app implying nowhere is safe
        # for a family with a food allergy, which is false and actively
        # harmful for exactly the families this product is supposed to help.
        #
        # Fixed to match the accessibility pattern instead: never hide a
        # restaurant for missing dietary data. Annotate it with which
        # restrictions are CONFIRMED safe (explicit matching tag) and which
        # are UNCONFIRMED (no data either way — needs a call ahead, not an
        # assumption in either direction) — see flag_unverified_dietary_safety().
        for r in rows:
            if r["category"] != "restaurant":
                continue
            tags = r["dietary_tags"] or []
            r["dietary_confirmed"] = [d for d in dietary_restrictions if d in tags]
            r["dietary_unconfirmed"] = [d for d in dietary_restrictions if d not in tags]

    return rows


def search_eligible_activities(
    destination_id: str,
    trip_id: str,
    category: str | None = None,
    exclude_outdoor: bool = False,
) -> list[dict]:
    """Hard-filters activities by accessibility, age, and diet BEFORE any semantic ranking.

    This ordering is deliberate: a well-matched activity that isn't step-free
    or nut-free isn't a near-miss, it's disqualifying. See docs/design.md
    section 5 (Context engineering).

    Use this for browsing/listing by category. For "find activities matching
    what this family is actually interested in," use
    search_activities_by_interest() instead, which adds semantic ranking on
    top of these same hard filters.
    """
    query = "SELECT * FROM activities WHERE destination_id = %s"
    params: list = [destination_id]

    if exclude_outdoor:
        query += " AND is_outdoor = FALSE"

    if category:
        query += " AND category = %s"
        params.append(category)

    rows = db.fetch_all(query, tuple(params))
    return _apply_hard_filters(rows, trip_id)


def _keyword_fallback_rank(rows: list[dict], query_text: str, top_k: int) -> list[dict]:
    """Poor-man's relevance ranking, used ONLY when Vector Search is
    unreachable — scores each activity by how many query words appear in
    its name/category/description, then sorts by that score.

    Without this, the fallback was returning whatever rows Postgres happened
    to return first, with no relevance to query_text at all — since
    restaurants vastly outnumber every other category in real data (70:1 or
    worse), that meant a query for "museum" silently returned restaurants
    instead, every time, until Vector Search is deployed. Not a substitute
    for real semantic search, but meaningfully better than arbitrary row
    order — a "museum" query now actually prefers rows whose category or
    name contains "museum".
    """
    query_words = {w for w in re.findall(r"\w+", query_text.lower()) if len(w) > 2}
    if not query_words:
        return rows[:top_k]

    def score(row: dict) -> int:
        haystack = " ".join(
            filter(None, [row.get("name", ""), row.get("category", ""), row.get("description") or ""])
        ).lower()
        return sum(1 for w in query_words if w in haystack)

    scored = sorted(rows, key=score, reverse=True)
    return scored[:top_k]


def search_activities_by_interest(
    trip_id: str,
    destination_id: str,
    query_text: str,
    top_k: int = 10,
    exclude_outdoor: bool = False,
) -> list[dict]:
    """Semantic search over the activities Vector Search index, restricted to
    hard-eligible activities for this trip's travelers. This is the "retrieve
    suitable activities based on interests" half of context engineering that
    was previously missing entirely — search_eligible_activities() alone
    only does column filtering, never anything based on meaning.

    `query_text` should describe what the family is after — pass the trip's
    stated interests/notes (see get_trip_interests) plus, when relevant,
    current conditions ("indoor activity, it's raining" / "outdoor, good
    weather") so retrieval reflects both interests AND conditions, per the
    product brief. Falls back to a keyword-relevance ranking (see
    _keyword_fallback_rank) if the vector index is unreachable, so a Vector
    Search outage degrades gracefully instead of returning arbitrary,
    query-irrelevant results.
    """
    ranked_ids = retrieval.semantic_search_activities(query_text, destination_id, top_k=top_k * 2)

    if not ranked_ids:
        # Vector Search unreachable/empty — degrade to keyword relevance
        # rather than whatever order the database happens to return.
        rows = search_eligible_activities(destination_id, trip_id, exclude_outdoor=exclude_outdoor)
        return _keyword_fallback_rank(rows, query_text, top_k)

    rows = db.fetch_all(
        "SELECT * FROM activities WHERE activity_id = ANY(%s)", (ranked_ids,)
    )
    rows_by_id = {r["activity_id"]: r for r in rows}
    # Preserve the vector search's relevance order — the SQL ANY() above does not.
    ordered_rows = [rows_by_id[i] for i in ranked_ids if i in rows_by_id]

    eligible = _apply_hard_filters(ordered_rows, trip_id)
    if exclude_outdoor:
        eligible = [r for r in eligible if not r["is_outdoor"]]

    return eligible[:top_k]


def get_weather_and_air_quality(destination_id: str, forecast_date: date) -> list[dict]:
    return db.fetch_all(
        """
        SELECT hour, temp_c, precipitation_prob, wind_kph, aqi, pm25, uv_index, pollen_level
        FROM weather_snapshots
        WHERE destination_id = %s AND forecast_date = %s
        ORDER BY hour
        """,
        (destination_id, forecast_date),
    )


def create_itinerary_item(
    trip_id: str,
    activity_id: str,
    day_date: date,
    start_time: time,
    end_time: time,
) -> str:
    """Adds a new activity to the itinerary. This was the actual missing
    piece behind "generate a day-by-day itinerary" — reschedule_item() could
    only ever move a row that already existed; nothing could create the
    first one. Returns the new item_id.
    """
    return db.execute_returning_id(
        """
        INSERT INTO itinerary_items (trip_id, activity_id, day_date, start_time, end_time, status)
        VALUES (%s, %s, %s, %s, %s, 'planned')
        RETURNING item_id
        """,
        (trip_id, activity_id, day_date, start_time, end_time),
        id_column="item_id",
    )


def delete_itinerary_item(trip_id: str, item_id: str, reason: str) -> None:
    """Removes an item from the itinerary — the "remove" from "add, remove,
    or move itinerary items," which previously had no tool at all.

    Logs with item_id=NULL rather than the deleted item's id: agent_decisions
    has a foreign key into itinerary_items, so a decision row can't reference
    a row that no longer exists (and logging before deleting doesn't help —
    the FK would then block the delete instead). The removed activity's name
    gets folded into the explanation text so the audit trail stays
    meaningful without depending on a reference that can't survive the delete.
    """
    item = db.fetch_one(
        """
        SELECT a.name AS activity_name
        FROM itinerary_items i
        JOIN activities a ON a.activity_id = i.activity_id
        WHERE i.item_id = %s
        """,
        (item_id,),
    )
    activity_name = item["activity_name"] if item else "an itinerary item"

    db.execute("DELETE FROM itinerary_items WHERE item_id = %s", (item_id,))
    log_decision(trip_id, None, "remove", "user_request", f"Removed '{activity_name}': {reason}")


def reschedule_item(
    trip_id: str,
    item_id: str,
    new_day_date: date,
    new_start_time: time,
    new_end_time: time,
    trigger: str,
    explanation: str,
) -> None:
    """Moves an itinerary item and logs the decision to agent_decisions.

    trigger must be one of the CHECK-constrained values in schema.sql
    (rain_forecast, high_aqi, nap_conflict, walk_budget_exceeded,
    unverified_accessibility, user_request).
    """
    db.execute(
        """
        UPDATE itinerary_items
        SET day_date = %s, start_time = %s, end_time = %s,
            status = 'rescheduled', rescheduled_reason = %s
        WHERE item_id = %s
        """,
        (new_day_date, new_start_time, new_end_time, explanation, item_id),
    )
    log_decision(trip_id, item_id, "reschedule", trigger, explanation)


def log_decision(trip_id: str, item_id: str | None, decision_type: str, trigger: str, explanation: str) -> None:
    db.execute(
        """
        INSERT INTO agent_decisions (trip_id, item_id, decision_type, trigger, explanation)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (trip_id, item_id, decision_type, trigger, explanation),
    )


def flag_unverified_accessibility(trip_id: str, item_id: str, activity_name: str) -> None:
    """Called when an itinerary item's accessibility data came from OSM but has
    never been human-verified. Surfacing this honestly matters more for a
    family/accessibility product than a confident-but-possibly-wrong claim.
    """
    log_decision(
        trip_id,
        item_id,
        "accessibility_flag",
        "unverified_accessibility",
        f"Accessibility info for '{activity_name}' comes from OpenStreetMap and hasn't "
        "been manually verified — worth a quick check or call ahead before you rely on it.",
    )


def flag_unverified_dietary_safety(
    trip_id: str, item_id: str, activity_name: str, unconfirmed_restrictions: list[str]
) -> None:
    """Called when a restaurant on the itinerary has no confirmed data for
    one or more of this trip's dietary restrictions (see the
    dietary_unconfirmed field _apply_hard_filters() adds to restaurant
    results). This is the dietary equivalent of
    flag_unverified_accessibility() — a restaurant with no allergy tagging
    is NOT the same as a restaurant confirmed safe, and the app should never
    imply otherwise for something this serious.
    """
    restrictions_text = ", ".join(r.replace("_", " ") for r in unconfirmed_restrictions)
    log_decision(
        trip_id,
        item_id,
        "dietary_flag",
        "unverified_dietary_safety",
        f"We couldn't confirm '{activity_name}' is safe for: {restrictions_text}. "
        "This isn't a warning that it's unsafe — we just don't have data either way. "
        "Worth calling ahead or checking the menu before you go.",
    )


def build_packing_item(
    trip_id: str, traveler_id: str | None, item_name: str, category: str, reason: str
) -> None:
    db.execute(
        """
        INSERT INTO packing_items (trip_id, traveler_id, item_name, category, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (trip_id, traveler_id, item_name, category, reason),
    )
