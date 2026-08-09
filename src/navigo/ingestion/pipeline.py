"""End-to-end destination ingestion: geocode -> Wikimedia -> weather/AQI -> Overpass POIs.

This is the shared logic behind both:
  - notebooks/01_ingest_seed_destinations.py (the Databricks notebook / job entrypoint)
  - direct local testing from a terminal, e.g.:
        python -c "
        import sys; sys.path.insert(0, 'src')
        from navigo.ingestion.pipeline import upsert_destination, refresh_weather, refresh_poi
        dest_id = upsert_destination('Edinburgh')
        print('destination_id:', dest_id)
        "

Kept as one module rather than duplicated in the notebook so local runs and
the scheduled job are always exercising identical code, not two copies that
can quietly drift apart.
"""

from __future__ import annotations

from navigo.db import client as db
from navigo.ingestion import open_meteo, overpass, wikimedia


def upsert_destination(name: str) -> str:
    """Geocodes a destination name, fetches its Wikimedia summary, and
    inserts/updates it in the `destinations` table. Returns the destination_id.
    """
    geo = open_meteo.geocode(name)
    if geo is None:
        raise ValueError(f"Could not geocode destination: {name}")

    summary = wikimedia.get_destination_summary(geo.name)

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
    """Fetches hourly weather + air quality and writes rows to `weather_snapshots`.
    Returns the number of rows written.

    Batched into a single connection via execute_many() rather than looping
    db.execute() per row — looping was opening ~168 separate connections to
    Lakebase for one week of hourly data, which is the main reason a seed
    call can feel like it's hanging.
    """
    weather_rows = open_meteo.get_hourly_weather(latitude, longitude)
    aqi_rows = open_meteo.get_air_quality(latitude, longitude)
    merged = open_meteo.merge_weather_and_air_quality(weather_rows, aqi_rows)

    params_list = [
        (
            destination_id, row["forecast_date"], row["hour"], row["temp_c"],
            row["precipitation_prob"], row["wind_kph"], row["aqi"], row["pm25"],
            row["uv_index"], row["pollen_level"],
        )
        for row in merged
    ]
    db.execute_many(
        """
        INSERT INTO weather_snapshots
            (destination_id, forecast_date, hour, temp_c, precipitation_prob,
             wind_kph, aqi, pm25, uv_index, pollen_level)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (destination_id, forecast_date, hour)
        DO UPDATE SET
            temp_c = EXCLUDED.temp_c,
            precipitation_prob = EXCLUDED.precipitation_prob,
            wind_kph = EXCLUDED.wind_kph,
            aqi = EXCLUDED.aqi,
            pm25 = EXCLUDED.pm25,
            uv_index = EXCLUDED.uv_index,
            pollen_level = EXCLUDED.pollen_level,
            captured_at = now()
        """,
        params_list,
    )
    return len(merged)


def _enrich_descriptions_from_wikimedia(pois: list[dict], latitude: float, longitude: float) -> None:
    """Mutates `pois` in place: for attraction/museum POIs with no description,
    tries to match them by name against nearby Wikipedia articles (geosearch)
    and fills in a summary. Silently skipped (not raised) on any Wikimedia
    failure — POI ingestion from Overpass shouldn't fail because a narrative
    enrichment step timed out.
    """
    needs_enrichment = [
        p for p in pois
        if p["category"] in ("attraction", "museum") and not p.get("description")
    ]
    if not needs_enrichment:
        return

    try:
        nearby = wikimedia.get_nearby_attractions(latitude, longitude)
    except Exception:
        return  # best-effort — don't let a Wikimedia hiccup break POI ingestion

    nearby_by_title = {n["title"].lower(): n["title"] for n in nearby}

    for poi in needs_enrichment:
        matched_title = nearby_by_title.get(poi["name"].lower())
        if matched_title is None:
            continue
        try:
            poi["description"] = wikimedia.get_summary(matched_title)
        except Exception:
            continue  # leave description as-is (None) rather than fail the whole batch


def refresh_poi(destination_id: str, latitude: float, longitude: float) -> int:
    """Fetches family/accessibility-tagged POIs from Overpass and inserts new
    ones into `activities`. Returns the number of POI candidates processed
    (not necessarily all inserted — existing rows by name are left alone, so
    manually-verified accessibility data never gets silently overwritten).

    Batched: one query to fetch all existing names for this destination,
    then one execute_many() for the new rows — instead of a
    fetch_one()+execute() round trip pair per individual POI.

    Attraction/museum descriptions Overpass left blank get a best-effort
    enrichment from Wikimedia's geosearch (see wikimedia.get_nearby_attractions)
    — this is what satisfies "Wikimedia... nearby attractions" from the
    product brief, complementing Overpass's structured accessibility tags
    rather than replacing them. Best-effort and non-fatal: if Wikimedia is
    unreachable, POIs still get written, just without the extra description.
    """
    pois = overpass.fetch_family_pois(latitude, longitude)
    if not pois:
        return 0

    existing_rows = db.fetch_all(
        "SELECT name FROM activities WHERE destination_id = %s", (destination_id,)
    )
    existing_names = {r["name"] for r in existing_rows}

    new_pois = [p for p in pois if p["name"] not in existing_names]
    _enrich_descriptions_from_wikimedia(new_pois, latitude, longitude)

    params_list = [
        (
            destination_id, poi["name"], poi["category"], poi["description"],
            poi["is_outdoor"], poi["latitude"], poi["longitude"], poi["osm_wheelchair"],
            poi["has_accessible_toilet"], poi["has_changing_table"], poi["has_highchairs"],
            poi["stroller_friendly"], poi["dietary_tags"], poi["source"],
        )
        for poi in new_pois
    ]
    db.execute_many(
        """
        INSERT INTO activities
            (destination_id, name, category, description, is_outdoor, latitude, longitude,
             osm_wheelchair, has_accessible_toilet, has_changing_table, has_highchairs,
             stroller_friendly, dietary_tags, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        params_list,
    )
    return len(pois)


def all_active_destinations() -> list[dict]:
    """Destinations attached to a trip that's still 'planning' or 'active' —
    used by the scheduled refresh jobs, which only refresh what's actually
    in use rather than every destination ever seeded.
    """
    return db.fetch_all(
        """
        SELECT DISTINCT d.destination_id, d.latitude, d.longitude
        FROM destinations d
        JOIN trips t ON t.home_base_destination_id = d.destination_id
        WHERE t.status IN ('planning', 'active')
        """
    )


def seed_destination(name: str) -> dict:
    """Convenience wrapper: geocodes + seeds weather/AQI + POIs for a brand
    new destination in one call. Returns a small summary dict for logging.
    """
    dest_id = upsert_destination(name)
    dest = db.fetch_one(
        "SELECT latitude, longitude FROM destinations WHERE destination_id = %s", (dest_id,)
    )
    # Cast: Postgres NUMERIC columns come back as Decimal via psycopg, not
    # float — fix it once here so every downstream call gets clean floats,
    # rather than relying only on overpass._bbox's defensive cast.
    latitude, longitude = float(dest["latitude"]), float(dest["longitude"])
    n_weather = refresh_weather(dest_id, latitude, longitude)
    n_poi = refresh_poi(dest_id, latitude, longitude)
    return {
        "destination_id": dest_id,
        "weather_rows": n_weather,
        "poi_candidates": n_poi,
    }
