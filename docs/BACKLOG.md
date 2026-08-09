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

- [ ] **`create_itinerary_item()`** — no tool currently inserts new `itinerary_items` rows; only `reschedule_item()` (moves existing ones) exists. Without this, "generate a day-by-day itinerary" has nothing to write with.
- [ ] **`delete_itinerary_item()`** — "remove" from the brief's "add, remove, or move" isn't built
- [ ] **`search_activities_by_interest()`** — the Vector Search index gets built (`02_build_vector_index.py`) but nothing ever queries it. `search_eligible_activities()` only does hard SQL filtering, no semantic retrieval. This is the actual "retrieve based on interests" requirement from the design doc, currently unimplemented.
- [ ] Embed `travelers.sensory_notes` (user notes) into the vector index — currently only `destinations`/`activities` text gets embedded, per the design doc's "embed... user notes" requirement
- [ ] Wire weather/AQI into retrieval automatically, rather than relying on the LLM to remember to pass `exclude_outdoor=True` itself
- [ ] Extend `agent.py`'s tool-call loop from single-round to multi-round (currently one request → tool calls → one follow-up; a real planning conversation will need more turns than that)
- [ ] Decide whether rescheduling should ever run proactively (a scheduled check against fresh weather data) vs. only reactively when the user asks in chat — currently it's chat-triggered only
- [ ] Wire `flag_unverified_accessibility()` into the itinerary-building flow — the function exists but nothing calls it yet

## Phase D — Streamlit UI
- [ ] Wire the "Save trip" button to actually insert into `trips` and `travelers` (currently a stub with an `st.info()` placeholder)
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
