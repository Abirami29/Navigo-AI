"""Navigo — Databricks App UI.

Deployed via resources/apps/navigo_app.yml. Run locally with:
    streamlit run src/navigo/app/streamlit_app.py

Trip setup and the itinerary view are wired to Lakebase for real. "Ask
Navigo" and "Why it changed" were already functional. See docs/BACKLOG.md
Phase D for anything still ahead.
"""

from datetime import date

import streamlit as st

from navigo.agent import tools
from navigo.agent.agent import run_agent_turn
from navigo.db import client as db
from navigo.ingestion.pipeline import get_or_create_destination

st.set_page_config(page_title="Navigo", page_icon="🧭", layout="wide")

st.title("🧭 Navigo")
st.caption("A weather-aware, kid-first, accessibility-first family holiday planner.")


def show_error(friendly_message: str, exc: Exception) -> None:
    """Shows a friendly message by default, with the real error available in
    an expander rather than dumping a raw traceback into the page. Every
    risky operation (DB calls, the agent call) goes through this instead of
    letting exceptions hit Streamlit's default full-traceback display.
    """
    st.error(friendly_message)
    with st.expander("Technical details"):
        st.exception(exc)


# A trip saved in this tab becomes the default trip_id everywhere else in the
# app. These three keys ARE the widgets' state (not just an initial value) —
# see the Save trip handler below for why that distinction matters.
for _key in ("itinerary_trip_id", "chat_trip_id", "log_trip_id"):
    if _key not in st.session_state:
        st.session_state[_key] = ""

tab_setup, tab_itinerary, tab_chat, tab_log = st.tabs(
    ["👨‍👩‍👧‍👦 Trip setup", "🗓️ Itinerary", "💬 Ask Navigo", "📋 Why it changed"]
)

with tab_setup:
    st.subheader("Trip details")
    trip_name = st.text_input("Trip name", placeholder="e.g. Half-term in Edinburgh")
    destination_name = st.text_input(
        "Destination",
        placeholder="e.g. Edinburgh",
        help="Plain city name works best — 'Edinburgh, UK' can fail to geocode. "
             "New destinations get fully seeded on save (weather, AQI, accessibility "
             "data) — this can take up to a minute or so for a new city.",
    )
    col1, col2 = st.columns(2)
    start_date = col1.date_input("Start date", value=date.today())
    end_date = col2.date_input("End date", value=date.today())

    st.subheader("What kind of trip is this?")
    st.caption(
        "This is the search_activities_by_interest() query the agent uses — "
        "see docs/design.md section 5. It's a preference, not a hard filter."
    )
    trip_interests_raw = st.text_area(
        "Interests",
        placeholder="e.g. museums, castles, animals, hands-on science exhibits, local food",
        help="Comma-separated, or just a sentence — this feeds the agent's search, not a fixed list.",
    )
    trip_notes = st.text_area(
        "Anything else worth knowing?",
        placeholder="e.g. \"we love hands-on science museums, not big on crowds\"",
    )

    st.subheader("Who's coming")
    st.caption(
        "This drives scheduling, walking limits, accessibility filtering, and the "
        "packing list — these are hard constraints the agent must respect, not preferences."
    )

    if "traveler_rows" not in st.session_state:
        st.session_state.traveler_rows = 1

    for i in range(st.session_state.traveler_rows):
        with st.expander(f"Traveler {i + 1}", expanded=(i == 0)):
            c1, c2, c3 = st.columns(3)
            c1.text_input("Label", key=f"label_{i}", placeholder="e.g. Leo (age 4)")
            c2.number_input("Age (years)", key=f"age_{i}", min_value=0, max_value=110, step=1)
            c3.selectbox(
                "Mobility need", ["none", "wheelchair", "stroller", "limited_walking"], key=f"mobility_{i}"
            )
            c4, c5 = st.columns(2)
            c4.number_input("Max walk (minutes)", key=f"walk_{i}", min_value=0, max_value=300, step=5)
            c5.text_input("Dietary restrictions (comma-separated)", key=f"diet_{i}")
            st.caption(
                "Optional scheduled break — a nap, a lunch window, medication time, "
                "anything the agent shouldn't book activities over. Leave blank if none."
            )
            c6, c7 = st.columns(2)
            c6.time_input("Break start", key=f"nap_start_{i}", value=None)
            c7.time_input("Break end", key=f"nap_end_{i}", value=None)
            st.text_area("Sensory / other notes", key=f"notes_{i}", placeholder="e.g. avoid loud/crowded venues after 3pm")

    if st.button("+ Add another traveler"):
        st.session_state.traveler_rows += 1
        st.rerun()

    if st.button("Save trip", type="primary"):
        if not trip_name or not destination_name:
            st.error("Trip name and destination are both required.")
        elif start_date > end_date:
            st.error("Start date must be before end date.")
        else:
            destination_lookup_failed = False
            try:
                with st.spinner(f"Looking up {destination_name}... (seeding real weather/accessibility "
                                 "data if this is a new destination — can take up to a minute)"):
                    destination_id, was_seeded = get_or_create_destination(destination_name)
            except Exception as exc:
                destination_id, was_seeded = None, False
                destination_lookup_failed = True
                show_error(
                    f"Couldn't look up '{destination_name}' — this is usually a network issue "
                    "reaching Open-Meteo/Wikimedia/Overpass, or a Lakebase connection problem. "
                    "Try again in a moment.",
                    exc,
                )

            if destination_id is None:
                if not destination_lookup_failed:
                    st.error(
                        f"Couldn't find '{destination_name}' — try a plain city name without "
                        "a country suffix (e.g. 'Edinburgh' rather than 'Edinburgh, UK')."
                    )
            else:
                try:
                    if was_seeded:
                        st.success(f"Seeded {destination_name} with real weather and accessibility data.")

                    user_id = db.execute_returning_id(
                        "INSERT INTO users (display_name) VALUES (%s) RETURNING user_id",
                        (trip_name,),
                        id_column="user_id",
                    )
                    trip_interests = [i.strip() for i in trip_interests_raw.split(",") if i.strip()]
                    trip_id = db.execute_returning_id(
                        """
                        INSERT INTO trips (user_id, trip_name, start_date, end_date,
                                            home_base_destination_id, interests, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING trip_id
                        """,
                        (user_id, trip_name, start_date, end_date, destination_id,
                         trip_interests, trip_notes or None),
                        id_column="trip_id",
                    )

                    traveler_params = []
                    for i in range(st.session_state.traveler_rows):
                        label = st.session_state.get(f"label_{i}", "").strip()
                        if not label:
                            continue  # skip blank traveler rows rather than saving empty ones
                        diet_raw = st.session_state.get(f"diet_{i}", "")
                        dietary_restrictions = [d.strip() for d in diet_raw.split(",") if d.strip()]
                        traveler_params.append((
                            trip_id,
                            label,
                            st.session_state.get(f"age_{i}") or None,
                            st.session_state.get(f"mobility_{i}", "none"),
                            st.session_state.get(f"walk_{i}") or None,
                            st.session_state.get(f"nap_start_{i}"),
                            st.session_state.get(f"nap_end_{i}"),
                            st.session_state.get(f"notes_{i}") or None,
                            dietary_restrictions,
                        ))

                    if traveler_params:
                        db.execute_many(
                            """
                            INSERT INTO travelers (trip_id, label, age_years, mobility_need,
                                                    max_walk_minutes, nap_window_start, nap_window_end,
                                                    sensory_notes, dietary_restrictions)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            traveler_params,
                        )

                    # Directly overwrite the OTHER tabs' widget-bound session_state
                    # keys, not just a separate variable — a widget with a `key`
                    # ignores any `value=` you pass it once it's already rendered
                    # once, so this direct assignment is what actually makes the
                    # trip_id show up in the other three tabs. This is the fix for
                    # the trip_id previously never populating anywhere else.
                    # str() is required here, not optional: psycopg returns
                    # Postgres UUID columns as uuid.UUID objects, not plain
                    # strings — Streamlit's text_input widget requires an
                    # actual str for its value, and assigning a UUID object
                    # directly fails with a TypeError deep inside Streamlit's
                    # protobuf serialization, not an obviously-Navigo error.
                    st.session_state["itinerary_trip_id"] = str(trip_id)
                    st.session_state["chat_trip_id"] = str(trip_id)
                    st.session_state["log_trip_id"] = str(trip_id)

                    st.success(
                        f"Trip saved! trip_id = {trip_id} — filled into the other tabs "
                        f"({len(traveler_params)} traveler(s) saved)."
                    )
                except Exception as exc:
                    show_error(
                        "Saved the destination, but something went wrong writing the trip/travelers "
                        "to Lakebase. The destination data itself is fine either way.",
                        exc,
                    )

with tab_itinerary:
    st.subheader("Day-by-day itinerary")
    itinerary_trip_id = st.text_input("Trip ID", key="itinerary_trip_id")
    if itinerary_trip_id:
        try:
            items = db.fetch_all(
                """
                SELECT i.item_id, i.day_date, i.start_time, i.end_time, i.status,
                       i.rescheduled_reason, a.name, a.category, a.osm_wheelchair
                FROM itinerary_items i
                JOIN activities a ON a.activity_id = i.activity_id
                WHERE i.trip_id = %s
                ORDER BY i.day_date, i.start_time
                """,
                (itinerary_trip_id,),
            )
        except Exception as exc:
            items = None
            show_error("Couldn't load the itinerary — check the Trip ID is correct.", exc)

        if items is not None and not items:
            st.info(
                "Nothing on the itinerary yet — use the 'Ask Navigo' tab to ask the agent "
                "to plan something, e.g. \"find a museum activity for tomorrow.\""
            )
        elif items:
            current_day = None
            for item in items:
                if item["day_date"] != current_day:
                    current_day = item["day_date"]
                    st.markdown(f"### {current_day.strftime('%A, %d %B %Y')}")

                access_note = " ⚠️ *unverified accessibility*" if item["osm_wheelchair"] == "unknown" else ""
                status_note = f" _(rescheduled: {item['rescheduled_reason']})_" if item["status"] == "rescheduled" else ""

                row_cols = st.columns([5, 1])
                row_cols[0].markdown(
                    f"**{item['start_time'].strftime('%H:%M')}–{item['end_time'].strftime('%H:%M')}** · "
                    f"{item['name']} _{item['category']}_{access_note}{status_note}"
                )
                if row_cols[1].button("🗑️ Remove", key=f"remove_{item['item_id']}"):
                    try:
                        tools.delete_itinerary_item(
                            itinerary_trip_id, str(item["item_id"]),
                            "Removed directly from the itinerary page.",
                        )
                        st.rerun()
                    except Exception as exc:
                        show_error("Couldn't remove that item.", exc)

                with st.expander("✏️ Change time"):
                    edit_cols = st.columns([1, 1, 1, 1])
                    new_day = edit_cols[0].date_input(
                        "Day", value=item["day_date"], key=f"day_{item['item_id']}"
                    )
                    new_start = edit_cols[1].time_input(
                        "Start", value=item["start_time"], key=f"start_{item['item_id']}"
                    )
                    new_end = edit_cols[2].time_input(
                        "End", value=item["end_time"], key=f"end_{item['item_id']}"
                    )
                    if edit_cols[3].button("Save", key=f"save_{item['item_id']}"):
                        try:
                            tools.reschedule_item(
                                itinerary_trip_id, str(item["item_id"]),
                                new_day, new_start, new_end,
                                trigger="user_request",
                                explanation="Time changed directly from the itinerary page.",
                            )
                            st.rerun()
                        except Exception as exc:
                            show_error("Couldn't reschedule that item.", exc)
            st.divider()

with tab_chat:
    st.subheader("Ask Navigo")
    trip_id = st.text_input("Trip ID", key="chat_trip_id")

    if "chat_histories" not in st.session_state:
        st.session_state.chat_histories = {}

    if trip_id:
        if trip_id not in st.session_state.chat_histories:
            st.session_state.chat_histories[trip_id] = []
        history = st.session_state.chat_histories[trip_id]
        for msg in history:
            st.chat_message(msg["role"]).write(msg["content"])

    user_message = st.chat_input("e.g. \"Move tomorrow's castle visit if it's going to rain\"")
    if user_message and trip_id:
        history = st.session_state.chat_histories[trip_id]
        try:
            with st.spinner("Navigo is thinking..."):
                # Passing the real conversation history is what makes "here
                # are 5 options, which would you like?" followed by "add the
                # second one" actually work — without this, every message
                # started a brand-new conversation with zero memory of what
                # was said before, so a follow-up selection had nothing to
                # refer back to. This was a real, load-bearing gap, not
                # just a nice-to-have.
                reply = run_agent_turn(trip_id, user_message, history=history)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": reply})
            st.rerun()
        except Exception as exc:
            show_error(
                "Navigo couldn't complete that request — this is usually a Model Serving "
                "connection issue (check DATABRICKS_HOST/TOKEN and the endpoint name in .env).",
                exc,
            )

with tab_log:
    st.subheader("Why it changed")
    st.caption("Every reschedule, swap, and accessibility flag the agent makes, in plain language.")
    trip_id_log = st.text_input("Trip ID", key="log_trip_id")
    if trip_id_log:
        try:
            decisions = db.fetch_all(
                "SELECT decision_type, trigger, explanation, created_at "
                "FROM agent_decisions WHERE trip_id = %s ORDER BY created_at DESC",
                (trip_id_log,),
            )
        except Exception as exc:
            decisions = None
            show_error("Couldn't load the decision log — check the Trip ID is correct.", exc)

        if decisions is not None and not decisions:
            st.write("No decisions logged yet for this trip.")
        elif decisions:
            for d in decisions:
                st.markdown(f"**{d['decision_type']}** · _{d['trigger']}_ · {d['created_at']}")
                st.write(d["explanation"])
                st.divider()
