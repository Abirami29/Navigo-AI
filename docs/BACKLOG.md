# Navigo — Post-first-pass backlog

Everything below was surfaced while getting the initial scaffold running locally.
Grouped by what blocks what, not by importance — do Phase A before anything else,
the rest can move around based on what you want to prove out next.

---

## Phase A — Finish proving out ingestion locally
*Nothing past this point is trustworthy until these are confirmed.*

- [ ] Test `wikimedia.get_summary()` against a real city (untested as of last check)
- [ ] Confirm `overpass.fetch_family_pois()` works after the `User-Agent` header fix — re-run and check you get real POI rows back, not another 406/empty list
- [ ] Test `open_meteo.get_hourly_weather()` and `open_meteo.get_air_quality()` directly (only `geocode()` has been proven so far)
- [ ] Run the full `upsert_destination()` flow end-to-end for one city from your terminal (not the notebook yet) — this is the first point all four ingestion pieces run together
- [ ] Once that works, run `01_ingest_seed_destinations.py` inside an actual Databricks notebook to confirm the notebook environment itself works, separately from the code

## Phase B — Close the geocoding gap
- [ ] `geocode()` currently takes Open-Meteo's top match blindly — ambiguous names (York, Cambridge, Springfield) can silently resolve to the wrong place with no error
- [ ] Fix: request `count=5` instead of `count=1`, add an optional `country_hint` param, filter results by it when provided
- [ ] Decide the UX for this in the app — silently pick top match, or ask the user to disambiguate when multiple strong matches exist

## Phase C — Missing agent capabilities (the actual planning logic)
*This is the biggest real gap — right now the agent can't build or edit a plan from scratch.*

- [x] **`create_itinerary_item()`** — done (src/navigo/agent/tools.py)
- [x] **`delete_itinerary_item()`** — done, logs with item_id=NULL + activity name folded into the explanation (agent_decisions.item_id is a FK into itinerary_items, can't reference a deleted row)
- [x] **`search_activities_by_interest()`** — done (src/navigo/agent/retrieval.py + tools.py). Queries the Vector Search index, degrades to hard-filter-only browsing if the index is unreachable.
- [ ] Embed `travelers.sensory_notes` into the vector index — deliberately NOT done this way. Redesigned instead: sensory_notes/interests are used as the *query* text at search time (see `get_trip_interests` + the agent's system prompt), not embedded as separate indexed documents. This is the correct RAG pattern — notes describe what the family wants, not a venue, so they don't belong in the same corpus as destination/activity descriptions.
- [x] Wire weather/AQI into retrieval — the system prompt now explicitly instructs the agent to check weather before finalizing outdoor plans and to fold current conditions into the `search_activities_by_interest` query text. Still LLM-enforced, not code-enforced — see note below.
- [x] Extend `agent.py`'s tool-call loop to multi-round — done, capped at `_MAX_TOOL_ROUNDS = 12` per turn as a safety valve
- [ ] Proactive (scheduled, non-chat-triggered) rescheduling — still not built, still a real design decision to make later
- [x] Wire `flag_unverified_accessibility()` into the flow — added to the agent's tool list and system prompt instructions, not auto-triggered in Python (relies on the LLM calling it)

**New this pass, not originally in the backlog:**
- [x] `trips.interests` / `trips.notes` columns — nothing previously captured "preferences" at all
- [x] Wikimedia `get_nearby_attractions()` (geosearch) — the brief named "nearby attractions" as a Wikimedia responsibility; only descriptions were ever implemented. Now used as a best-effort description enrichment for Overpass POIs that came back with no description.
- [x] Streamlit trip-setup form now has interest/notes fields (still not wired to the DB — that's Phase D)

**Known limitation worth naming honestly**: constraint-following (accessibility, diet, weather-awareness) is enforced two different ways here — hard filters in `tools.py` (`_apply_hard_filters`) are enforced in code and cannot be bypassed, but weather-awareness and interest-matching are enforced by the system prompt, which means they're *strong instructions to the model*, not guarantees. An LLM can still ignore a "check the weather first" instruction. If that turns out to matter in practice, the fix is moving more of that logic into Python (e.g., have `create_itinerary_item` itself check weather and refuse/warn on a bad pairing) rather than trusting the prompt alone.

## Phase D — Streamlit UI
- [ ] Wire the "Save trip" button to actually insert into `trips` and `travelers` (now needs to include the interests/notes fields too — form UI is ready, DB call still isn't)
- [ ] Build the itinerary timeline view — query `itinerary_items` joined to `activities`, currently just a placeholder message
- [ ] Replace the manual "type in a Trip ID" text inputs with a real trip picker once trips can be saved

## Phase E — Databricks deployment
- [ ] Decide the Lakebase-credentials-in-Databricks approach (env vars via app.yaml/job config vs. `dbutils.secrets` in notebooks vs. something else) — deliberately deferred earlier, still needs a decision before jobs/app can run in Databricks
- [ ] Install Databricks CLI, generate a PAT with the scopes discussed (Workspace, Jobs, Apps, Secrets, Model Serving Inference)
- [ ] Create the Lakebase instance and secret scope in the actual workspace (if not already done)
- [ ] Deploy `resources/jobs/*.yml` and `resources/apps/*.yml` via `databricks bundle deploy`, or recreate them by hand in the UI (Workflows, Apps) if skipping the CLI
- [ ] Confirm a real Model Serving endpoint name and put it in `.env` / job config — `agent.py` currently points at a placeholder endpoint name
- [ ] Run `02_build_vector_index.py` inside a Databricks notebook (it can't run locally — needs `spark` and `VectorSearchClient`)

## Phase F — Testing & hardening
- [ ] Extend `tests/test_ingestion.py` to cover `wikimedia.py` and `overpass.py` logic, not just `open_meteo.py`/`overpass.py`'s pure functions
- [ ] Add tests for `agent/tools.py` (mock the DB layer — right now these are entirely untested against real logic)
- [ ] Add a GitHub Actions workflow to run `pytest` on every push (mentioned earlier, not yet created)
- [ ] Add `docs/SETUP.md` capturing the step-by-step walkthrough from this conversation, so it lives with the repo instead of only in chat history
- [ ] Overpass usage etiquette: confirm the scheduled `poi_sync_job` is the only thing calling Overpass in practice — don't let ad-hoc "seed a destination" calls during development turn into de facto live traffic against the public instance

---

**Suggested order if you want one:** finish Phase A this session → Phase C (it's the actual core feature) → Phase D just enough to demo Phase C → Phase E once there's something worth deploying → Phase B and F can be picked up opportunistically alongside any of the above.

## Phase G — Conversational-first redesign (parked, not started)

User's stated vision, captured directly rather than paraphrased into something smaller:

> "I feel it should go from where they would like to go and then the date and then optional restrictions. It's not nice to have to input everything for everyone. The optional restrictions should be parsed by AI to feed into the planning... I want it to be free text to get the interests, restrictions, and what they are looking for. Frontend could be the itinerary page and Ask Navigo page. In Ask Navigo they should type what they want as free text. From that the agent should ask more questions if necessary and give options for the trip itinerary. Once chosen, then can be added to the itinerary page. From the itinerary page they can reorder, remove, or add their own itinerary."

Breaking that into concrete pieces for whenever this gets picked up:

- [ ] Replace the structured trip-setup form with a conversational intake: destination + dates first, then free-text "who's coming and any restrictions" parsed by the agent into `travelers` rows — not a rigid per-person form up front
- [ ] Group composition shouldn't assume "the whole family every time" — needs to work equally well for "us three plus the kids" and "four adults, a friends' trip," without forcing users through an "exclude people" flow
- [ ] "Ask Navigo" becomes the primary planning surface: free-text request → agent asks clarifying questions if it needs them → presents options → user picks → *then* it lands on the itinerary. (The immediate version of "present options before committing" was fixed directly in the system prompt as of the "find activities" bug — this Phase G item is the fuller vision: multi-turn clarification before even the first option gets presented, not just gating the final commit step.)
- [ ] Itinerary page becomes editable directly — reorder, remove, or manually add items without going through chat at all
- [ ] Real transit/travel-mode data (nearest public transport, how the family gets around) was raised alongside this — explicitly decided NOT to build as structured data (no Overpass transit query, no schema field) and instead folded into the free-text/clarifying-question flow above, where the agent can ask and reason about it conversationally rather than needing a real data source

This is a genuinely different shape of product than the current form-based scaffold — worth treating as its own design pass rather than incremental patches to the existing Streamlit form.
