"""Open-Meteo clients: geocoding, hourly weather, and air quality.

No API key required for noncommercial use under Open-Meteo's free limits.
Docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations

from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from navigo.config import EXTERNAL_APIS

_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))


@dataclass
class GeocodeResult:
    name: str
    country: str | None
    latitude: float
    longitude: float


@_RETRY
def geocode(destination_name: str) -> GeocodeResult | None:
    """Resolves a destination name to coordinates via Open-Meteo Geocoding API."""
    resp = requests.get(
        f"{EXTERNAL_APIS.open_meteo_geocoding_url}/v1/search",
        params={"name": destination_name, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    top = results[0]
    return GeocodeResult(
        name=top["name"],
        country=top.get("country"),
        latitude=top["latitude"],
        longitude=top["longitude"],
    )


@_RETRY
def get_hourly_weather(latitude: float, longitude: float, days: int = 7) -> list[dict]:
    """Returns hourly forecast rows: date, hour, temp_c, precipitation_prob, wind_kph."""
    resp = requests.get(
        f"{EXTERNAL_APIS.open_meteo_base_url}/v1/forecast",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
            "forecast_days": days,
            "timezone": "auto",
        },
        timeout=10,
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    rows = []
    for i, timestamp in enumerate(hourly["time"]):
        date_str, time_str = timestamp.split("T")
        rows.append(
            {
                "forecast_date": date_str,
                "hour": int(time_str.split(":")[0]),
                "temp_c": hourly["temperature_2m"][i],
                "precipitation_prob": hourly["precipitation_probability"][i],
                "wind_kph": hourly["wind_speed_10m"][i],
            }
        )
    return rows


@_RETRY
def get_air_quality(latitude: float, longitude: float, days: int = 7) -> list[dict]:
    """Returns hourly AQI/PM2.5/UV/pollen rows aligned to forecast_date + hour."""
    resp = requests.get(
        f"{EXTERNAL_APIS.open_meteo_air_quality_url}/v1/air-quality",
        params={
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "us_aqi,pm2_5,uv_index,grass_pollen",
            "forecast_days": days,
            "timezone": "auto",
        },
        timeout=10,
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    rows = []
    for i, timestamp in enumerate(hourly["time"]):
        date_str, time_str = timestamp.split("T")
        grass_pollen = hourly.get("grass_pollen", [None] * len(hourly["time"]))[i]
        rows.append(
            {
                "forecast_date": date_str,
                "hour": int(time_str.split(":")[0]),
                "aqi": hourly["us_aqi"][i],
                "pm25": hourly["pm2_5"][i],
                "uv_index": hourly["uv_index"][i],
                "pollen_level": _pollen_bucket(grass_pollen),
            }
        )
    return rows


def _pollen_bucket(grass_pollen: float | None) -> str | None:
    """Very rough bucketing of grass pollen grains/m3 into a human label."""
    if grass_pollen is None:
        return None
    if grass_pollen < 10:
        return "low"
    if grass_pollen < 50:
        return "moderate"
    return "high"


def merge_weather_and_air_quality(weather_rows: list[dict], aqi_rows: list[dict]) -> list[dict]:
    """Joins weather + air quality rows on (forecast_date, hour) into weather_snapshots rows."""
    aqi_index = {(r["forecast_date"], r["hour"]): r for r in aqi_rows}
    merged = []
    for w in weather_rows:
        key = (w["forecast_date"], w["hour"])
        a = aqi_index.get(key, {})
        merged.append({**w, "aqi": a.get("aqi"), "pm25": a.get("pm25"),
                       "uv_index": a.get("uv_index"), "pollen_level": a.get("pollen_level")})
    return merged
