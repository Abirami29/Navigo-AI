# Navigo

**A weather-aware, kid-first, accessibility-first family holiday planning agent — built entirely on Databricks Free Edition.**

Navigo helps families plan a day-by-day holiday itinerary that actually accounts for who's coming: nap windows, wheelchair or stroller access, food allergies, and realistic walking limits for kids — then re-plans automatically when rain, air quality, or the day's pace stop working, and explains every change in plain language.

## Why

Planning a family holiday means juggling weather, kid-friendly venues, accessibility info, allergy-safe restaurants, and nap schedules across a dozen browser tabs. Navigo puts all of that in one place and hands the juggling to an agent.

## Stack

| Layer | Technology |
|---|---|
| Transactional data | [Lakebase](https://docs.databricks.com/aws/en/lakebase/) (managed Postgres) |
| Governance | Unity Catalog |
| Semantic search | Databricks Vector Search |
| Agent orchestration | Mosaic AI Agent Framework / Agent Bricks |
| LLM | Databricks-hosted foundation model via Model Serving |
| Scheduled ingestion | Lakeflow Jobs |
| Observability | MLflow 3 tracing |
| UI | Databricks Apps (Streamlit) |
| External data | Open-Meteo (geocoding, weather, air quality), Wikimedia, OpenStreetMap Overpass API |

All of the above is available on **Databricks Free Edition** at no cost (serverless, fair-use quotas apply).

## Repo layout

```
navigo-ai/
├── databricks.yml              # Databricks Asset Bundle definition
├── resources/                  # Bundle resources: jobs + app
│   ├── jobs/
│   │   ├── weather_refresh_job.yml
│   │   └── poi_sync_job.yml
│   └── apps/
│       └── navigo_app.yml
├── src/navigo/
│   ├── config.py                # env-driven settings
│   ├── db/
│   │   ├── schema.sql           # Lakebase (Postgres) schema
│   │   └── client.py            # DB connection + query helpers
│   ├── ingestion/
│   │   ├── open_meteo.py        # geocoding, weather, air quality
│   │   ├── overpass.py          # accessibility / kid-friendly POI tags
│   │   └── wikimedia.py         # destination + attraction summaries
│   ├── agent/
│   │   ├── tools.py             # agent tool functions
│   │   └── agent.py             # agent orchestration
│   └── app/
│       └── streamlit_app.py     # Databricks App UI
├── notebooks/
│   ├── 00_setup_lakebase.py     # run schema.sql against Lakebase
│   ├── 01_ingest_seed_destinations.py
│   └── 02_build_vector_index.py
├── tests/
│   └── test_ingestion.py
└── docs/
    └── design.md                # full product/architecture design doc
```

## Getting started (Databricks Free Edition)

1. **Create a Lakebase instance** in your Free Edition workspace (Compute → Lakebase → Create).
2. **Set environment variables / secrets** (see `.env.example`): `LAKEBASE_HOST`, `LAKEBASE_PORT`, `LAKEBASE_DB`, `LAKEBASE_USER`, `LAKEBASE_PASSWORD`.
3. **Run the schema**: open `notebooks/00_setup_lakebase.py` in a Databricks notebook and run it — it applies `src/navigo/db/schema.sql`.
4. **Seed a destination**: run `notebooks/01_ingest_seed_destinations.py` with a destination name (e.g. `"Edinburgh, UK"`) to pull geocoding, weather, AQI, Wikimedia summary, and Overpass accessibility POIs.
5. **Build the vector index**: run `notebooks/02_build_vector_index.py` to embed destinations/activities into Databricks Vector Search.
6. **Deploy the jobs + app** via Databricks Asset Bundles:
   ```bash
   databricks bundle deploy
   databricks bundle run poi_sync_job
   databricks bundle run weather_refresh_job
   ```
7. **Run the app locally** (optional, before deploying as a Databricks App):
   ```bash
   pip install -r requirements.txt
   streamlit run src/navigo/app/streamlit_app.py
   ```

## Status

This repo is a working scaffold: schema, ingestion clients, and agent tool stubs are functional; the agent orchestration and UI are intentionally minimal starting points meant to be built out. See `docs/design.md` for the full product and architecture design this scaffold implements.

## License

MIT — see `LICENSE`.
