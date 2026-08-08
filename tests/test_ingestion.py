"""Unit tests for pure parsing/merging logic in the ingestion modules.

These deliberately avoid live network calls (no API key needed, but no CI
should depend on external service uptime). Use `responses` or `pytest-mock`
to test the request-making functions themselves as a next step.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from navigo.ingestion.open_meteo import merge_weather_and_air_quality
from navigo.ingestion.overpass import _build_query, _infer_category, _yes_no


def test_merge_weather_and_air_quality_joins_on_date_and_hour():
    weather_rows = [
        {"forecast_date": "2026-08-10", "hour": 9, "temp_c": 18.0, "precipitation_prob": 10, "wind_kph": 12},
    ]
    aqi_rows = [
        {"forecast_date": "2026-08-10", "hour": 9, "aqi": 42, "pm25": 8.1, "uv_index": 5.0, "pollen_level": "moderate"},
    ]

    merged = merge_weather_and_air_quality(weather_rows, aqi_rows)

    assert len(merged) == 1
    assert merged[0]["temp_c"] == 18.0
    assert merged[0]["aqi"] == 42
    assert merged[0]["pollen_level"] == "moderate"


def test_merge_handles_missing_air_quality_row():
    weather_rows = [{"forecast_date": "2026-08-10", "hour": 9, "temp_c": 18.0,
                      "precipitation_prob": 10, "wind_kph": 12}]
    merged = merge_weather_and_air_quality(weather_rows, [])
    assert merged[0]["aqi"] is None


def test_infer_category_maps_osm_tags_to_navigo_categories():
    assert _infer_category({"leisure": "playground"}) == "playground"
    assert _infer_category({"tourism": "museum"}) == "museum"
    assert _infer_category({"amenity": "restaurant"}) == "restaurant"
    assert _infer_category({"shop": "bakery"}) is None


def test_yes_no_parses_osm_boolean_style_values():
    assert _yes_no("yes") is True
    assert _yes_no("no") is False
    assert _yes_no(None) is None


def test_build_query_includes_bbox_and_all_category_tags():
    query = _build_query(55.9533, -3.1883)
    assert "leisure" in query
    assert "tourism" in query
    assert "amenity" in query
    assert "55." in query  # bbox latitude present
