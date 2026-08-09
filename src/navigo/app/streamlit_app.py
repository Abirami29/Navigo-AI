"""Navigo — Databricks App UI.

Deployed via resources/apps/navigo_app.yml. Run locally with:
    streamlit run src/navigo/app/streamlit_app.py

Two tabs by design: Ask Navigo (the only way to create/plan trips now —
trip creation moved from a form into conversational tool calls, see
navigo.agent.tools.create_trip) and Itinerary (view + direct edit). The
decision log ("why it changed") lives as an expander inside Itinerary
rather than its own tab, so the information isn't lost, just not a
separate top-level tab. See docs/BACKLOG.md Phase G for the fuller
conversational-intake vision this is a step toward.
"""

import streamlit as st

from navigo.agent import tools
from navigo.agent.agent import run_agent_turn
from navigo.db import client as db

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


if "chat_trip_id" not in st.session_state:
    st.session_state["chat_trip_id"] = ""
if "itinerary_trip_id" not in st.session_state:
    st.session_state["itinerary_trip_id"] = ""
if "chat_histories" not in st.session_state:
    st.session_state.chat_histories = {}  # keyed by trip_id, or "__new__" before one exists

# Consumed here, before either text_input(key=...) below gets instantiated —
# writing to a widget's own session_state key AFTER that widget has already
# rendered in the current script run raises StreamlitAPIException, even
# right before a st.rerun(). Setting it here, on the fresh run triggered by
# that rerun, is the one ordering Streamlit actually allows.
if "pending_trip_id" in st.session_state:
    _new_id = st.session_state.pop("pending_trip_id")
    st.session_state["chat_trip_id"] = _new_id
    st.session_state["itinerary_trip_id"] = _new_id

tab_chat, tab_itinerary = st.tabs(["💬 Ask Navigo", "🗓️ Itinerary"])

with tab_chat:
    st.subheader("Ask Navigo")
    st.caption(
        "Start a brand-new trip by just describing it — e.g. \"Plan a trip to "
        "Edinburgh, Aug 10-15, me and my 4 year old, and I need step-free access.\" "
        "Or paste an existing Trip ID below to keep planning one you already started."
    )

    trip_id_input = st.text_input("Trip ID (leave blank to start a new trip)", key="chat_trip_id")
    active_trip_id = trip_id_input or None

    # Conversations that haven't created a trip yet live under a shared
    # "__new__" bucket; once create_trip() succeeds mid-conversation, that
    # history gets moved over to the real trip_id (see below) so nothing
    # said before the trip existed is lost.
    history_key = active_trip_id or "__new__"
    if history_key not in st.session_state.chat_histories:
        st.session_state.chat_histories[history_key] = []
    history = st.session_state.chat_histories[history_key]

    for msg in history:
        st.chat_message(msg["role"]).write(msg["content"])

    user_message = st.chat_input(
        "e.g. \"Plan a trip to Edinburgh, Aug 10-15\" or \"find a museum for tomorrow\""
    )
    if user_message:
        try:
            with st.spinner("Navigo is thinking..."):
                result = run_agent_turn(active_trip_id, user_message, history=history)

            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": result["content"]})

            new_trip_id = result.get("new_trip_id")
            if new_trip_id and not active_trip_id:
                # A trip was just created in this turn — move the "__new__"
                # conversation over to its real trip_id. The actual widget
                # key updates happen via pending_trip_id at the top of the
                # script on the next run (see above) — writing directly to
                # chat_trip_id/itinerary_trip_id here would raise
                # StreamlitAPIException, since this tab's own text_input for
                # chat_trip_id has already rendered earlier in this run.
                st.session_state.chat_histories[new_trip_id] = st.session_state.chat_histories.pop("__new__")
                st.session_state["pending_trip_id"] = new_trip_id

            st.rerun()
        except Exception as exc:
            show_error(
                "Navigo couldn't complete that request — this is usually a Model Serving "
                "connection issue (check DATABRICKS_HOST/TOKEN and the endpoint name in .env).",
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

        # "Why it changed" folded in here as an expander rather than its own
        # tab, per the 2-tab layout — same data, just not top-level anymore.
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
