"""Navigo — Databricks App UI.

Deployed via resources/apps/navigo_app.yml. Run locally with:
    streamlit run src/navigo/app/streamlit_app.py

This is a minimal starting UI: trip + traveler setup, itinerary view, and the
agent's decision log (the "why" trail — see docs/design.md section 4,
agent_decisions table). Intended to be built out, not a finished product.
"""

import streamlit as st

from navigo.agent.agent import run_agent_turn
from navigo.db import client as db

st.set_page_config(page_title="Navigo", page_icon="🧭", layout="wide")

st.title("🧭 Navigo")
st.caption("A weather-aware, kid-first, accessibility-first family holiday planner.")

tab_setup, tab_itinerary, tab_chat, tab_log = st.tabs(
    ["👨‍👩‍👧‍👦 Trip setup", "🗓️ Itinerary", "💬 Ask Navigo", "📋 Why it changed"]
)

with tab_setup:
    st.subheader("Trip details")
    trip_name = st.text_input("Trip name", placeholder="e.g. Half-term in Edinburgh")
    destination_name = st.text_input("Destination", placeholder="e.g. Edinburgh, UK")
    col1, col2 = st.columns(2)
    start_date = col1.date_input("Start date")
    end_date = col2.date_input("End date")

    st.subheader("What kind of trip is this?")
    st.caption(
        "This is the search_activities_by_interest() query the agent uses — "
        "see docs/design.md section 5. It's a preference, not a hard filter."
    )
    trip_interests = st.multiselect(
        "Interests",
        ["museums", "nature & outdoors", "castles & history", "animals & wildlife",
         "beaches", "playgrounds", "theme parks", "art & culture", "local food"],
        placeholder="Pick what this family enjoys",
    )
    trip_notes = st.text_area(
        "Anything else worth knowing?",
        placeholder="e.g. \"we love hands-on science museums, not big on crowds\"",
    )

    st.subheader("Who's coming")
    st.caption(
        "This drives nap scheduling, walking limits, accessibility filtering, and the "
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
            c6, c7 = st.columns(2)
            c6.time_input("Nap start", key=f"nap_start_{i}", value=None)
            c7.time_input("Nap end", key=f"nap_end_{i}", value=None)
            st.text_area("Sensory / other notes", key=f"notes_{i}", placeholder="e.g. avoid loud/crowded venues after 3pm")

    if st.button("+ Add another traveler"):
        st.session_state.traveler_rows += 1
        st.rerun()

    if st.button("Save trip", type="primary"):
        st.info(
            "Wire this button to insert into `trips` (including interests/notes above) "
            "and `travelers` via navigo.db.client — left as a build-out step (Phase D "
            "in docs/BACKLOG.md). The interests/traveler fields above are ready to be "
            "persisted; this button just doesn't call the DB yet."
        )

with tab_itinerary:
    st.subheader("Day-by-day itinerary")
    st.caption("Populated once a trip exists and the agent has generated a plan.")
    st.info("Query `itinerary_items` joined to `activities` for the selected trip and render as a day timeline.")

with tab_chat:
    st.subheader("Ask Navigo")
    trip_id = st.text_input("Trip ID", help="Temporary manual input until trip selection UI is built.")
    user_message = st.chat_input("e.g. \"Move tomorrow's castle visit if it's going to rain\"")
    if user_message and trip_id:
        with st.spinner("Navigo is thinking..."):
            reply = run_agent_turn(trip_id, user_message)
        st.chat_message("user").write(user_message)
        st.chat_message("assistant").write(reply)

with tab_log:
    st.subheader("Why it changed")
    st.caption("Every reschedule, swap, and accessibility flag the agent makes, in plain language.")
    trip_id_log = st.text_input("Trip ID ", key="log_trip_id")
    if trip_id_log:
        decisions = db.fetch_all(
            "SELECT decision_type, trigger, explanation, created_at "
            "FROM agent_decisions WHERE trip_id = %s ORDER BY created_at DESC",
            (trip_id_log,),
        )
        if not decisions:
            st.write("No decisions logged yet for this trip.")
        for d in decisions:
            st.markdown(f"**{d['decision_type']}** · _{d['trigger']}_ · {d['created_at']}")
            st.write(d["explanation"])
            st.divider()
