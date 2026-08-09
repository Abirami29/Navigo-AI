"""OpenStreetMap Overpass API client — the piece that makes Navigo
accessibility- and kid-aware rather than a generic trip planner.

Pulls POIs (attractions, restaurants, playgrounds, museums) within a bounding
box around a destination, along with tags relevant to families:
  wheelchair, toilets:wheelchair, changing_table, highchair, diet:*

Etiquette: Overpass's public instances are a shared community resource. This
client is designed to be called from the scheduled poi_sync_job, not live
per user request — see resources/jobs/poi_sync_job.yml.

Mirror fallback: overpass-api.de has been intermittently rejecting requests
with 406 Not Acceptable since the operator started filtering traffic that
looks programmatic (documented widely in the OSM community forum through
2025-2026), and a User-Agent header alone isn't reliably enough to avoid it
anymore. This client tries your configured URL first, then falls back to
known-working public mirrors, rather than failing outright on one server's
bad day. See https://community.openstreetmap.org/t/overpass-api-error-406

Docs: https://wiki.openstreetmap.org/wiki/Overpass_API
"""

from __future__ import annotations

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from navigo.config import EXTERNAL_APIS

_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))

_REQUEST_HEADERS = {
    "User-Agent": "navigo-ai/0.1 (family holiday planner demo; contact via GitHub repo)",
    "Accept": "application/json",
}

# Fallback mirrors, tried in order if the configured URL (first entry) fails.
# Deduplicated at call time in case OVERPASS_API_URL is already one of these.
_FALLBACK_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

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


def _candidate_urls() -> list[str]:
    """Configured URL first, then fallback mirrors, de-duplicated in order."""
    urls = [EXTERNAL_APIS.overpass_api_url]
    for mirror in _FALLBACK_MIRRORS:
        if mirror not in urls:
            urls.append(mirror)
    return urls


@_RETRY
def _post_query(query: str) -> dict:
    """Posts the query to the first candidate URL that responds successfully,
    falling back through the mirror list on 4xx/5xx or connection errors.
    """
    last_error: Exception | None = None
    for url in _candidate_urls():
        try:
            resp = requests.post(url, data={"data": query}, timeout=30, headers=_REQUEST_HEADERS)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            last_error = exc
            continue
    # Every mirror failed — let tenacity's @_RETRY retry the whole sweep
    # (all mirrors again) before finally raising.
    raise last_error


def fetch_family_pois(latitude: float, longitude: float) -> list[dict]:
    """Fetches POIs near a destination with family/accessibility-relevant tags.

    Returns a list of dicts shaped to map directly onto `activities` columns.
    """
    query = _build_query(latitude, longitude)
    data = _post_query(query)
    elements = data.get("elements", [])

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
