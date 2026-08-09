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

import sys
sys.path.insert(0, "../src")

dbutils.widgets.text("destination_name", "", "Destination name (seed mode only)")
dbutils.widgets.dropdown(
    "mode", "seed", ["seed", "refresh_weather_only", "refresh_poi_only"], "Mode"
)

destination_name = dbutils.widgets.get("destination_name")
mode = dbutils.widgets.get("mode")

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
