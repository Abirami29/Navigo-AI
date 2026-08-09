# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Seed / refresh a destination
# MAGIC
# MAGIC Run manually with a destination name to seed it for the first time (geocode +
# MAGIC weather + AQI + Wikimedia summary + Overpass POIs).
# MAGIC
# MAGIC Also used as the entrypoint for the scheduled Lakeflow jobs
# MAGIC (`resources/jobs/weather_refresh_job.yml`, `resources/jobs/poi_sync_job.yml`),
# MAGIC which pass `mode=refresh_weather_only` / `mode=refresh_poi_only` via the
# MAGIC `mode` widget to refresh existing destinations without re-seeding them.
# MAGIC
# MAGIC The actual ingestion logic lives in `navigo.ingestion.pipeline` rather than
# MAGIC here, so it's importable and testable from a plain terminal too — this
# MAGIC notebook is just the Databricks-specific entrypoint (widgets + scheduling)
# MAGIC around it.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

import os
import sys
sys.path.insert(0, "../src")

dbutils.widgets.text("destination_name", "", "Destination name (seed mode only)")
dbutils.widgets.dropdown(
    "mode", "seed", ["seed", "refresh_weather_only", "refresh_poi_only"], "Mode"
)
# Only genuinely non-sensitive Lakebase details as plain job widgets — port
# and database name carry no identifying information. Host, user, and
# password all come from the navigo_secrets scope instead (see below):
# a hostname embeds the workspace ID, so it's treated as sensitive too, not
# committed as a plain value in any job YAML.
dbutils.widgets.text("lakebase_port", "5432", "Lakebase port")
dbutils.widgets.text("lakebase_db", "navigo", "Lakebase database name")

destination_name = dbutils.widgets.get("destination_name")
mode = dbutils.widgets.get("mode")

# Populate LAKEBASE_* env vars before navigo.db.client is ever imported —
# this is the actual fix for a real deployment failure: jobs don't read
# .env, and nothing else in this project was wiring credentials into a JOB's
# execution environment (only the deployed App had that, via app.yaml). With
# LAKEBASE_HOST left empty, the resulting connection string was malformed
# enough that psycopg mistook "port=5432" for the hostname itself.
# Conditional on LAKEBASE_HOST being unset so this notebook still behaves
# identically when run locally/interactively with real env vars already set.
if not os.getenv("LAKEBASE_HOST"):
    os.environ["LAKEBASE_PORT"] = dbutils.widgets.get("lakebase_port")
    os.environ["LAKEBASE_DB"] = dbutils.widgets.get("lakebase_db")
    # All three fetched from the same navigo_secrets scope used by the
    # Databricks App — one shared source of truth for connection details,
    # never committed as plain values in any job/app YAML.
    os.environ["LAKEBASE_HOST"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_host")
    os.environ["LAKEBASE_USER"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_user")
    os.environ["LAKEBASE_PASSWORD"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_password")

# Diagnostic: print exactly what ended up in the env vars, before anything
# tries to connect with them. The same "port=5432 mistaken for host" error
# happened both before AND after adding the widget/secret injection above —
# this print is what tells us definitively whether the injection actually
# ran and used real values, instead of guessing a third time. Never prints
# the password itself, just whether it's empty.
print(f"LAKEBASE_HOST = {os.getenv('LAKEBASE_HOST')!r}")
print(f"LAKEBASE_PORT = {os.getenv('LAKEBASE_PORT')!r}")
print(f"LAKEBASE_DB = {os.getenv('LAKEBASE_DB')!r}")
print(f"LAKEBASE_USER = {os.getenv('LAKEBASE_USER')!r}")
print(f"LAKEBASE_PASSWORD is set: {bool(os.getenv('LAKEBASE_PASSWORD'))}")

# COMMAND ----------

from navigo.db import client as db
from navigo.ingestion.pipeline import (
    all_active_destinations,
    refresh_poi,
    refresh_weather,
    seed_destination,
)

# COMMAND ----------

if mode == "seed":
    if not destination_name:
        raise ValueError("destination_name widget is required in seed mode.")
    result = seed_destination(destination_name)
    print(f"Seeded {destination_name}: {result}")

elif mode == "refresh_weather_only":
    for dest in all_active_destinations():
        n = refresh_weather(dest["destination_id"], dest["latitude"], dest["longitude"])
        print(f"Refreshed weather for {dest['destination_id']}: {n} rows")

elif mode == "refresh_poi_only":
    for dest in all_active_destinations():
        n = refresh_poi(dest["destination_id"], dest["latitude"], dest["longitude"])
        print(f"Refreshed POIs for {dest['destination_id']}: {n} candidates processed")
