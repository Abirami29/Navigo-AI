"""Tool functions exposed to the Navigo planning agent.

Each function here is a candidate for registration as an agent tool (e.g. via
Mosaic AI Agent Framework's @tool decorator or an OpenAI-style tool schema).
They are kept as plain, testable Python functions and wired into the agent
framework in agent.py, so the core logic isn't tied to any one framework.
"""

from __future__ import annotations

from datetime import date, time

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
    """
    accessibility_need = get_accessibility_requirement(trip_id)
    dietary_restrictions = get_dietary_restrictions(trip_id)

    query = "SELECT * FROM activities WHERE destination_id = %s"
    params: list = [destination_id]

    if accessibility_need in ("wheelchair", "stroller"):
        query += " AND osm_wheelchair != 'no'"

    if exclude_outdoor:
        query += " AND is_outdoor = FALSE"

    if category:
        query += " AND category = %s"
        params.append(category)

    rows = db.fetch_all(query, tuple(params))

    if dietary_restrictions:
        rows = [
            r
            for r in rows
            if r["category"] != "restaurant"
            or any(diet in (r["dietary_tags"] or []) for diet in dietary_restrictions)
        ]

    return rows


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
