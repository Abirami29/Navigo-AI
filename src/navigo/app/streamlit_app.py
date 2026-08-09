"""Navigo — Databricks App UI.

Deployed via resources/apps/navigo_app.yml. Run locally with:
    streamlit run src/navigo/app/streamlit_app.py

Three tabs: Plan a Trip (a 3-step inline flow — form, browse activities,
pick a time — replacing the earlier chat-only trip creation), Itinerary
(view + direct edit), Ask Navigo (chat, kept for follow-up adjustments
after a trip exists — see docs/BACKLOG.md Phase G for the fuller
conversational-intake vision this sits alongside).
"""

from datetime import date, timedelta

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
    an expander rather than dumping a raw traceback into the page.
    """
    st.error(friendly_message)
    with st.expander("Technical details"):
        st.exception(exc)


for _key, _default in [
    ("chat_trip_id", ""), ("itinerary_trip_id", ""),
    ("setup_step", "form"), ("setup_trip_id", None), ("setup_destination_id", None),
    ("browse_results", []), ("browse_query", ""),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {}  # keyed by trip_id, or "__new__" before one exists

# Consumed here, before any text_input(key=...) below gets instantiated —
# writing to a widget's own session_state key AFTER that widget has already
# rendered in the current script run raises StreamlitAPIException, even
# right before a st.rerun(). Setting it here, on the fresh run triggered by
# that rerun, is the one ordering Streamlit actually allows.
if "pending_trip_id" in st.session_state:
    _new_id = st.session_state.pop("pending_trip_id")
    st.session_state["chat_trip_id"] = _new_id
    st.session_state["itinerary_trip_id"] = _new_id

tab_setup, tab_itinerary, tab_chat = st.tabs(["🧭 Plan a Trip", "🗓️ Itinerary", "💬 Ask Navigo"])

with tab_setup:
    if st.session_state.setup_step == "form":
        st.subheader("Where and when?")
        col1, col2 = st.columns([2, 1])
        destination_name = col1.text_input(
            "🔍 Destination",
            placeholder="e.g. Edinburgh",
            help="Plain city name works best — 'Edinburgh, UK' can fail to look up.",
        )
        date_range = col2.date_input(
            "📅 Dates",
            value=(date.today(), date.today() + timedelta(days=4)),
        )

        st.subheader("Who's coming?")
        col3, col4 = st.columns(2)
        num_adults = col3.number_input("Adults", min_value=0, max_value=15, value=2, step=1)
        num_children = col4.number_input("Children", min_value=0, max_value=15, value=0, step=1)

        child_ages = []
        if num_children > 0:
            st.caption("Ages of the children (this drives age-appropriate suggestions):")
            age_cols = st.columns(min(int(num_children), 6))
            for i in range(int(num_children)):
                child_ages.append(age_cols[i % len(age_cols)].number_input(
                    f"Child {i + 1}", min_value=0, max_value=17, value=6, step=1, key=f"child_age_{i}",
                ))

        st.subheader("Anything we should know?")
        constraints_text = st.text_area(
            "Accessibility needs, allergies, mobility, or anything else",
            placeholder="e.g. \"Grandpa uses a wheelchair, my son has a peanut allergy\"",
            help="Free text — Navigo picks out the relevant details automatically.",
        )
        interests_text = st.text_area(
            "What kind of trip is this?",
            placeholder="e.g. museums, live music, hands-on science, castles",
        )

        if st.button("Save & find activities", type="primary"):
            if not destination_name:
                st.error("Destination is required.")
            elif not isinstance(date_range, tuple) or len(date_range) != 2:
                st.error("Pick both a start and end date.")
            else:
                start_date, end_date = date_range
                try:
                    with st.spinner(f"Looking up {destination_name}... (seeding real weather/accessibility "
                                     "data if this is a new destination — can take up to a minute)"):
                        destination_id, was_seeded = get_or_create_destination(destination_name)

                    if destination_id is None:
                        st.error(
                            f"Couldn't find '{destination_name}' — try a plain city name without "
                            "a country suffix (e.g. 'Edinburgh' rather than 'Edinburgh, UK')."
                        )
                    else:
                        if was_seeded:
                            st.success(f"Seeded {destination_name} with real weather and accessibility data.")

                        interests = [i.strip() for i in interests_text.split(",") if i.strip()]
                        constraints = tools.parse_constraints_text(constraints_text) if constraints_text else {
                            "mobility_need": "none", "dietary_restrictions": [],
                        }

                        user_id = db.execute_returning_id(
                            "INSERT INTO users (display_name) VALUES (%s) RETURNING user_id",
                            (f"{destination_name} trip",), id_column="user_id",
                        )
                        trip_id = db.execute_returning_id(
                            """
                            INSERT INTO trips (user_id, trip_name, start_date, end_date,
                                                home_base_destination_id, interests, notes)
                            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING trip_id
                            """,
                            (user_id, f"Trip to {destination_name}", start_date, end_date,
                             destination_id, interests, constraints_text or None),
                            id_column="trip_id",
                        )

                        # Applying the parsed mobility/dietary flags to every
                        # traveler row (not just "the right person") is
                        # deliberate, not sloppy — get_accessibility_requirement
                        # and get_dietary_restrictions both take the union/
                        # strictest across ALL travelers anyway, so the
                        # aggregate result is identical either way, without
                        # needing to guess which specific person the free
                        # text was describing.
                        traveler_params = []
                        for i in range(int(num_adults)):
                            traveler_params.append((
                                trip_id, f"Adult {i + 1}", None, constraints["mobility_need"],
                                None, None, None, None, constraints["dietary_restrictions"],
                            ))
                        for i, age in enumerate(child_ages):
                            traveler_params.append((
                                trip_id, f"Child {i + 1} (age {age})", age, constraints["mobility_need"],
                                None, None, None, None, constraints["dietary_restrictions"],
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

                        st.session_state.setup_trip_id = str(trip_id)
                        st.session_state.setup_destination_id = str(destination_id)
                        st.session_state.setup_step = "browse"
                        st.session_state["pending_trip_id"] = str(trip_id)
                        st.rerun()
                except Exception as exc:
                    show_error("Something went wrong saving the trip.", exc)

    elif st.session_state.setup_step == "browse":
        st.subheader("What sounds good?")
        trip_id = st.session_state.setup_trip_id
        destination_id = st.session_state.setup_destination_id

        query_text = st.text_input(
            "Search for activities",
            value=st.session_state.browse_query,
            placeholder="e.g. live music, museums, castles, animals",
        )

        if st.button("🔍 Find activities", type="primary") or (query_text and not st.session_state.browse_results):
            try:
                with st.spinner("Checking weather and searching..."):
                    # Fold current weather into the query, same principle as
                    # the agent's system prompt — options should reflect
                    # conditions, not just interests, and the reasoning
                    # should be visible, not silent.
                    today_weather = db.fetch_all(
                        "SELECT precipitation_prob, aqi FROM weather_snapshots "
                        "WHERE destination_id = %s AND forecast_date = %s ORDER BY hour LIMIT 1",
                        (destination_id, date.today()),
                    )
                    weather_note = ""
                    exclude_outdoor = False
                    if today_weather:
                        rain = today_weather[0]["precipitation_prob"] or 0
                        if rain and rain > 50:
                            weather_note = f" (rain forecast today, {rain}% chance — showing indoor options)"
                            exclude_outdoor = True

                    results = tools.search_activities_by_interest(
                        trip_id, destination_id, query_text or "family-friendly activities",
                        top_k=8, exclude_outdoor=exclude_outdoor,
                    )
                    st.session_state.browse_results = results
                    st.session_state.browse_query = query_text
                if weather_note:
                    st.caption(f"☁️{weather_note}")
            except Exception as exc:
                show_error("Couldn't search for activities.", exc)

        if st.session_state.browse_results:
            options = {
                f"{r['name']} — {r['category']}"
                + (" ⚠️ unverified accessibility" if r["osm_wheelchair"] == "unknown" else ""): r
                for r in st.session_state.browse_results
            }
            choice_label = st.selectbox("Choose one", list(options.keys()))
            chosen = options[choice_label]
            if chosen.get("description"):
                st.caption(chosen["description"][:300])

            if st.button("✅ Choose this activity", type="primary"):
                st.session_state.chosen_activity = chosen
                st.session_state.setup_step = "pick_time"
                st.rerun()

        if st.button("Skip — go straight to itinerary"):
            st.session_state["pending_trip_id"] = trip_id
            st.session_state.setup_step = "form"
            st.rerun()

    elif st.session_state.setup_step == "pick_time":
        st.subheader("When should this be?")
        chosen = st.session_state.chosen_activity
        st.markdown(f"**{chosen['name']}** · {chosen['category']}")

        col1, col2, col3 = st.columns(3)
        pick_day = col1.date_input("Day", value=date.today())
        pick_start = col2.time_input("Start time")
        pick_end = col3.time_input("End time")

        if st.button("💾 Add to itinerary", type="primary"):
            try:
                new_item_id = tools.create_itinerary_item(
                    st.session_state.setup_trip_id, chosen["activity_id"],
                    pick_day, pick_start, pick_end,
                )
                if chosen["osm_wheelchair"] == "unknown":
                    tools.flag_unverified_accessibility(
                        st.session_state.setup_trip_id, new_item_id, chosen["name"]
                    )
                st.success(f"Added {chosen['name']} to the itinerary!")
                st.session_state.browse_results = []
                st.session_state.browse_query = ""
                st.session_state.setup_step = "browse"
                st.rerun()
            except Exception as exc:
                show_error("Couldn't add that to the itinerary.", exc)

        if st.button("← Back to activities"):
            st.session_state.setup_step = "browse"
            st.rerun()

    if st.session_state.setup_step != "form":
        st.divider()
        if st.button("Start a completely new trip"):
            st.session_state.setup_step = "form"
            st.session_state.browse_results = []
            st.session_state.browse_query = ""
            st.rerun()

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
                "Nothing on the itinerary yet — use the 'Plan a Trip' tab or 'Ask Navigo' "
                "to add something."
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

        with st.expander("📋 Why it changed"):
            try:
                decisions = db.fetch_all(
                    "SELECT decision_type, trigger, explanation, created_at "
                    "FROM agent_decisions WHERE trip_id = %s ORDER BY created_at DESC",
                    (itinerary_trip_id,),
                )
            except Exception as exc:
                decisions = None
                show_error("Couldn't load the decision log.", exc)

            if decisions is not None and not decisions:
                st.write("No decisions logged yet for this trip.")
            elif decisions:
                for d in decisions:
                    st.markdown(f"**{d['decision_type']}** · _{d['trigger']}_ · {d['created_at']}")
                    st.write(d["explanation"])
                    st.divider()

with tab_chat:
    st.subheader("Ask Navigo")
    st.caption(
        "Use this for follow-up adjustments once a trip exists — reschedule things, "
        "ask questions, or plan freeform. To start a brand-new trip, use the 'Plan a Trip' tab."
    )

    trip_id_input = st.text_input("Trip ID", key="chat_trip_id")
    active_trip_id = trip_id_input or None

    history_key = active_trip_id or "__new__"
    if history_key not in st.session_state.chat_histories:
        st.session_state.chat_histories[history_key] = []
    history = st.session_state.chat_histories[history_key]

    for msg in history:
        st.chat_message(msg["role"]).write(msg["content"])

    user_message = st.chat_input("e.g. \"Move tomorrow's castle visit if it's going to rain\"")
    if user_message:
        try:
            with st.spinner("Navigo is thinking..."):
                result = run_agent_turn(active_trip_id, user_message, history=history)

            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": result["content"]})

            new_trip_id = result.get("new_trip_id")
            if new_trip_id and not active_trip_id:
                st.session_state.chat_histories[new_trip_id] = st.session_state.chat_histories.pop("__new__")
                st.session_state["pending_trip_id"] = new_trip_id

            st.rerun()
        except Exception as exc:
            show_error(
                "Navigo couldn't complete that request — this is usually a Model Serving "
                "connection issue (check DATABRICKS_HOST/TOKEN and the endpoint name in .env).",
                exc,
            )
