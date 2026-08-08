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
from navigo.ingestion import open_meteo, overpass, wikimedia


def upsert_destination(name: str) -> str:
    geo = open_meteo.geocode(name)
    if geo is None:
        raise ValueError(f"Could not geocode destination: {name}")

    summary = wikimedia.get_summary(geo.name)

    existing = db.fetch_one(
        "SELECT destination_id FROM destinations WHERE name = %s AND country = %s",
        (geo.name, geo.country),
    )
    if existing:
        destination_id = existing["destination_id"]
        db.execute(
            "UPDATE destinations SET wikimedia_summary = %s WHERE destination_id = %s",
            (summary, destination_id),
        )
    else:
        destination_id = db.execute_returning_id(
            """
            INSERT INTO destinations (name, country, latitude, longitude, wikimedia_summary)
            VALUES (%s, %s, %s, %s, %s) RETURNING destination_id
            """,
            (geo.name, geo.country, geo.latitude, geo.longitude, summary),
            id_column="destination_id",
        )
    return destination_id


def refresh_weather(destination_id: str, latitude: float, longitude: float) -> int:
    weather_rows = open_meteo.get_hourly_weather(latitude, longitude)
    aqi_rows = open_meteo.get_air_quality(latitude, longitude)
    merged = open_meteo.merge_weather_and_air_quality(weather_rows, aqi_rows)

    for row in merged:
        db.execute(
            """
            INSERT INTO weather_snapshots
                (destination_id, forecast_date, hour, temp_c, precipitation_prob,
                 wind_kph, aqi, pm25, uv_index, pollen_level)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                destination_id, row["forecast_date"], row["hour"], row["temp_c"],
                row["precipitation_prob"], row["wind_kph"], row["aqi"], row["pm25"],
                row["uv_index"], row["pollen_level"],
            ),
        )
    return len(merged)


def refresh_poi(destination_id: str, latitude: float, longitude: float) -> int:
    pois = overpass.fetch_family_pois(latitude, longitude)
    for poi in pois:
        existing = db.fetch_one(
            "SELECT activity_id FROM activities WHERE destination_id = %s AND name = %s",
            (destination_id, poi["name"]),
        )
        if existing:
            continue  # keep it simple for the mockup: don't overwrite manually-verified rows
        db.execute(
            """
            INSERT INTO activities
                (destination_id, name, category, description, is_outdoor, latitude, longitude,
                 osm_wheelchair, has_accessible_toilet, has_changing_table, has_highchairs,
                 stroller_friendly, dietary_tags, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                destination_id, poi["name"], poi["category"], poi["description"],
                poi["is_outdoor"], poi["latitude"], poi["longitude"], poi["osm_wheelchair"],
                poi["has_accessible_toilet"], poi["has_changing_table"], poi["has_highchairs"],
                poi["stroller_friendly"], poi["dietary_tags"], poi["source"],
            ),
        )
    return len(pois)


def all_active_destinations() -> list[dict]:
    return db.fetch_all(
        """
        SELECT DISTINCT d.destination_id, d.latitude, d.longitude
        FROM destinations d
        JOIN trips t ON t.home_base_destination_id = d.destination_id
        WHERE t.status IN ('planning', 'active')
        """
    )


# COMMAND ----------

if mode == "seed":
    if not destination_name:
        raise ValueError("destination_name widget is required in seed mode.")
    dest_id = upsert_destination(destination_name)
    dest = db.fetch_one("SELECT latitude, longitude FROM destinations WHERE destination_id = %s", (dest_id,))
    n_weather = refresh_weather(dest_id, dest["latitude"], dest["longitude"])
    n_poi = refresh_poi(dest_id, dest["latitude"], dest["longitude"])
    print(f"Seeded {destination_name}: destination_id={dest_id}, {n_weather} weather rows, {n_poi} POIs")

elif mode == "refresh_weather_only":
    for dest in all_active_destinations():
        n = refresh_weather(dest["destination_id"], dest["latitude"], dest["longitude"])
        print(f"Refreshed weather for {dest['destination_id']}: {n} rows")

elif mode == "refresh_poi_only":
    for dest in all_active_destinations():
        n = refresh_poi(dest["destination_id"], dest["latitude"], dest["longitude"])
        print(f"Refreshed POIs for {dest['destination_id']}: {n} candidates processed")
