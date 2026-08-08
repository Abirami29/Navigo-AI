"""OpenStreetMap Overpass API client — the piece that makes Navigo
accessibility- and kid-aware rather than a generic trip planner.

Pulls POIs (attractions, restaurants, playgrounds, museums) within a bounding
box around a destination, along with tags relevant to families:
  wheelchair, toilets:wheelchair, changing_table, highchair, diet:*

Etiquette: Overpass's public instance is a shared community resource. This
client is designed to be called from the scheduled poi_sync_job, not live
per user request — see resources/jobs/poi_sync_job.yml.
Docs: https://wiki.openstreetmap.org/wiki/Overpass_API
"""

from __future__ import annotations

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from navigo.config import EXTERNAL_APIS

_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))

# OSM amenity/tourism/leisure values we care about, mapped to Navigo's activity category
_CATEGORY_TAG_MAP = {
    "attraction": ["tourism=attraction", "tourism=viewpoint", "tourism=zoo", "tourism=theme_park"],
    "museum": ["tourism=museum"],
    "restaurant": ["amenity=restaurant", "amenity=cafe", "amenity=fast_food"],
    "playground": ["leisure=playground"],
    "outdoor": ["leisure=park", "leisure=nature_reserve", "leisure=garden"],
}

_BBOX_DEGREES = 0.09  # roughly ~10km radius, good enough for "in this town"


def _bbox(latitude: float, longitude: float, delta: float = _BBOX_DEGREES) -> str:
    south, north = latitude - delta, latitude + delta
    west, east = longitude - delta, longitude + delta
    return f"{south},{west},{north},{east}"


def _build_query(latitude: float, longitude: float) -> str:
    bbox = _bbox(latitude, longitude)
    all_tags = [tag for tags in _CATEGORY_TAG_MAP.values() for tag in tags]
    clauses = []
    for tag in all_tags:
        key, _, value = tag.partition("=")
        clauses.append(f'  node["{key}"="{value}"]({bbox});')
    tag_clauses = "\n".join(clauses)
    # Overpass QL: fetch nodes matching any of our category tags, with tag output
    return f"""
[out:json][timeout:25];
(
{tag_clauses}
);
out body;
"""


def _infer_category(tags: dict) -> str | None:
    for category, tag_defs in _CATEGORY_TAG_MAP.items():
        for tag_def in tag_defs:
            key, _, value = tag_def.partition("=")
            if tags.get(key) == value:
                return category
    return None


@_RETRY
def fetch_family_pois(latitude: float, longitude: float) -> list[dict]:
    """Fetches POIs near a destination with family/accessibility-relevant tags.

    Returns a list of dicts shaped to map directly onto `activities` columns.
    """
    query = _build_query(latitude, longitude)
    resp = requests.post(EXTERNAL_APIS.overpass_api_url, data={"data": query}, timeout=30)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])

    pois = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        category = _infer_category(tags)
        if not name or not category:
            continue

        wheelchair = tags.get("wheelchair", "unknown")
        if wheelchair not in ("yes", "limited", "no"):
            wheelchair = "unknown"

        dietary_tags = [
            key.split(":", 1)[1]
            for key, value in tags.items()
            if key.startswith("diet:") and value == "yes"
        ]

        pois.append(
            {
                "name": name,
                "category": category,
                "description": tags.get("description"),
                "is_outdoor": category in ("playground", "outdoor"),
                "latitude": el.get("lat"),
                "longitude": el.get("lon"),
                "osm_wheelchair": wheelchair,
                "has_accessible_toilet": _yes_no(tags.get("toilets:wheelchair")),
                "has_changing_table": _yes_no(tags.get("changing_table")),
                "has_highchairs": _yes_no(tags.get("highchair")),
                "stroller_friendly": _yes_no(tags.get("stroller")) if "stroller" in tags else None,
                "dietary_tags": dietary_tags,
                "source": "overpass",
            }
        )
    return pois


def _yes_no(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() == "yes"
