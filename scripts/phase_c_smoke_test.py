"""Phase C smoke test: seeds a test trip + travelers with real, mixed
constraints, then exercises every itinerary-agent tool function directly
against Lakebase.

This tests the TOOLS (src/navigo/agent/tools.py) directly, not the full LLM
agent loop (src/navigo/agent/agent.py) — that needs a deployed Model Serving
endpoint, which almost certainly isn't set up yet. This script proves the
plumbing underneath the agent actually works before wiring an LLM on top.

Prerequisites:
  - Lakebase schema applied
  - The Phase C migration applied (trips.interests/notes columns,
    agent_decisions decision_type CHECK updated to allow 'remove')
  - A destination already seeded — this looks up 'Edinburgh' by name below;
    change DESTINATION_NAME if you seeded a different city.

Run from the repo root:
    python scripts/phase_c_smoke_test.py
"""

import sys
import uuid
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from navigo.agent import tools
from navigo.db import client as db

DESTINATION_NAME = "Edinburgh"


def _step(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def run() -> None:
    _step("0. Look up the seeded destination")
    dest = db.fetch_one(
        "SELECT destination_id, name FROM destinations WHERE name = %s", (DESTINATION_NAME,)
    )
    if dest is None:
        raise SystemExit(
            f"No destination named '{DESTINATION_NAME}' found. Seed one first, e.g.:\n"
            f'  python -c "import sys; sys.path.insert(0,\'src\'); '
            f"from navigo.ingestion.pipeline import seed_destination; "
            f'print(seed_destination(\'{DESTINATION_NAME}\'))"'
        )
    destination_id = dest["destination_id"]
    print(f"Using destination: {dest['name']} ({destination_id})")

    _step("1. Create a test user + trip")
    user_id = db.execute_returning_id(
        "INSERT INTO users (display_name) VALUES (%s) RETURNING user_id",
        (f"Test Parent {uuid.uuid4().hex[:6]}",),
        id_column="user_id",
    )
    today = date.today()
    trip_id = db.execute_returning_id(
        """
        INSERT INTO trips (user_id, trip_name, start_date, end_date,
                            home_base_destination_id, interests, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING trip_id
        """,
        (
            user_id, "Edinburgh family half-term", today, today + timedelta(days=2),
            destination_id, ["museums", "castles & history", "animals & wildlife"],
            "We love hands-on science museums, not big on crowds.",
        ),
        id_column="trip_id",
    )
    print(f"Created trip: {trip_id}")

    _step("2. Add travelers with real, mixed constraints")
    db.execute_many(
        """
        INSERT INTO travelers (trip_id, label, age_years, mobility_need, max_walk_minutes,
                                nap_window_start, nap_window_end, sensory_notes, dietary_restrictions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (trip_id, "Mum", 38, "none", 240, None, None, None, []),
            (trip_id, "Grandpa (wheelchair user)", 71, "wheelchair", 90, None, None, None, []),
            (trip_id, "Leo (age 4)", 4, "stroller", 60, time(13, 0), time(14, 30),
             "gets overwhelmed by loud/crowded places", ["peanut_allergy"]),
        ],
    )
    print(
        "Added 3 travelers: Mum (no constraints), Grandpa (wheelchair, 90min walk budget), "
        "Leo (age 4, stroller, nap 1:00-2:30pm, peanut allergy, sensory-sensitive)"
    )

    _step("3. Read back the hard constraints exactly as the agent would see them")
    print("get_trip_interests:", tools.get_trip_interests(trip_id))
    print("get_accessibility_requirement (expect 'wheelchair' — the strictest):",
          tools.get_accessibility_requirement(trip_id))
    print("get_dietary_restrictions (expect ['peanut_allergy']):",
          tools.get_dietary_restrictions(trip_id))
    print("get_family_walk_budget (expect 60 — Leo's, the lowest/most restrictive):",
          tools.get_family_walk_budget(trip_id, today))
    print("get_nap_windows (expect Leo, 13:00-14:30):", tools.get_nap_windows(trip_id))

    _step("4. search_eligible_activities — hard filters only, no semantic ranking")
    eligible = tools.search_eligible_activities(destination_id, trip_id, category="restaurant")
    print(f"Eligible restaurants (must be wheelchair-accessible AND peanut-safe): {len(eligible)}")
    for r in eligible[:5]:
        print(f"  {r['name']} | wheelchair={r['osm_wheelchair']} | diet_tags={r['dietary_tags']}")
    if not eligible:
        print(
            "  (0 is plausible, not necessarily a bug — your earlier data check showed "
            "only ~14% of restaurants are tagged wheelchair=yes, and peanut-safe tagging "
            "is rarer still in OSM. This is honest data scarcity, not broken filtering.)"
        )

    _step("5. search_activities_by_interest — semantic search + the SAME hard filters")
    print(
        "(Falls back to hard-filter-only browsing if Vector Search isn't deployed yet — "
        "expected until you've done Phase E. Check which path it took below.)"
    )
    interest_results = tools.search_activities_by_interest(
        trip_id, destination_id, "hands-on science museum, indoor, good for young kids", top_k=5
    )
    print(f"Got {len(interest_results)} results:")
    for r in interest_results:
        print(f"  {r['name']} | {r['category']} | wheelchair={r['osm_wheelchair']}")

    _step("6. create_itinerary_item — the tool that didn't exist before this pass")
    if interest_results:
        chosen = interest_results[0]
        item_id = tools.create_itinerary_item(
            trip_id, chosen["activity_id"], today, time(10, 0), time(11, 30)
        )
        print(f"Created itinerary item {item_id} for '{chosen['name']}' at 10:00-11:30")

        if chosen["osm_wheelchair"] == "unknown":
            tools.flag_unverified_accessibility(trip_id, item_id, chosen["name"])
            print("Flagged as unverified accessibility (osm_wheelchair was 'unknown')")

        _step("7. delete_itinerary_item — the other tool that didn't exist before")
        tools.delete_itinerary_item(trip_id, item_id, "testing the delete tool")
        print(f"Deleted item {item_id}")
    else:
        print(
            "Skipping steps 6-7 — no eligible activities found to schedule. "
            "If this is unexpected, check step 4's note about data scarcity above."
        )

    _step("8. build_packing_item — unchanged, confirming it still works")
    tools.build_packing_item(
        trip_id, None, "Sun hats", "clothing", "UV index forecast is high this week"
    )
    print("Added a packing item")

    _step("9. agent_decisions — the full audit trail from this run")
    decisions = db.fetch_all(
        """
        SELECT decision_type, trigger, explanation
        FROM agent_decisions WHERE trip_id = %s ORDER BY created_at
        """,
        (trip_id,),
    )
    for d in decisions:
        print(f"  [{d['decision_type']}/{d['trigger']}] {d['explanation']}")
    if not decisions:
        print("  (none logged — expected if step 6 was skipped)")

    _step("Done")
    print(f"trip_id = {trip_id}")
    print(
        "Reuse this trip_id to keep testing tools.py functions directly, or in the "
        "Streamlit app's 'Ask Navigo' / 'Why it changed' tabs once Model Serving is deployed."
    )


if __name__ == "__main__":
    run()
