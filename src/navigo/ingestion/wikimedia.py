"""Wikimedia (MediaWiki Action API + Wikipedia REST API) client for
destination descriptions and nearby attractions.

Used to give the agent narrative context for the "why we picked this" text,
and as embedding input for semantic retrieval. Two separate Wikimedia
surfaces are used here for two different jobs:
  - Wikipedia REST API (api/rest_v1) — exact-title summary lookups
  - MediaWiki Action API (w/api.php) — full-text search and geosearch,
    used to resolve loose place names and to find attractions near a
    destination's coordinates (the "nearby attractions" requirement)
Docs: https://en.wikipedia.org/api/rest_v1/ and
      https://www.mediawiki.org/wiki/API:Main_page
"""

from __future__ import annotations

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from navigo.config import EXTERNAL_APIS

_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))

_REQUEST_HEADERS = {"User-Agent": "navigo-ai/0.1 (family holiday planner demo)"}

# MediaWiki's search endpoint — a different base URL from the summary REST API
# above. Used to resolve a plain place name to a real article title before
# fetching its summary, since /page/summary/{title} only does an exact
# title/redirect match with no fuzzy search of its own.
_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"

# MediaWiki's legacy Action API — used here specifically for geosearch
# (list=geosearch), which the newer REST API doesn't expose. Finds Wikipedia
# articles near a coordinate, which is how we satisfy "nearby attractions"
# from Wikimedia rather than relying on Overpass for everything.
_ACTION_API_URL = "https://en.wikipedia.org/w/api.php"


@_RETRY
def get_summary(title: str) -> str | None:
    """Returns a short plain-text summary for an EXACT Wikipedia page title
    (or a real redirect to one), or None if no such page exists.

    This only works when `title` is already a real article/redirect title —
    it does not search. For a plain place name that might not match exactly,
    use get_destination_summary() instead, which searches first.
    """
    resp = requests.get(
        f"{EXTERNAL_APIS.wikimedia_base_url}/page/summary/{requests.utils.quote(title)}",
        timeout=10,
        headers=_REQUEST_HEADERS,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return data.get("extract")


@_RETRY
def _search_best_title(query: str) -> str | None:
    """Searches Wikipedia and returns the top-matching article's real title,
    or None if nothing matched.
    """
    resp = requests.get(
        _SEARCH_URL,
        params={"q": query, "limit": 1},
        timeout=10,
        headers=_REQUEST_HEADERS,
    )
    resp.raise_for_status()
    pages = resp.json().get("pages", [])
    return pages[0]["title"] if pages else None


def get_destination_summary(place_name: str) -> str | None:
    """Resolves a plain place name to the best-matching Wikipedia article via
    search, then fetches its summary. This is what upsert_destination() uses
    (see navigo.ingestion.pipeline) — more robust than get_summary() for
    arbitrary geocoded names, since it doesn't require an exact
    title/redirect match, just a reasonable search hit.

    Still not perfect: for genuinely ambiguous single-word names (e.g. many
    "Lincoln"s worldwide), the top search result may not be the one you
    meant. Worth revisiting alongside the geocoding ambiguity fix in the
    backlog (Phase B) if that turns out to matter in practice.
    """
    resolved_title = _search_best_title(place_name)
    if resolved_title is None:
        return None
    return get_summary(resolved_title)


@_RETRY
def get_nearby_attractions(latitude: float, longitude: float, radius_m: int = 8000, limit: int = 15) -> list[dict]:
    """Finds Wikipedia articles geographically near a destination —
    Wikimedia's own version of "nearby attractions," independent of
    Overpass. Complements rather than replaces Overpass: Overpass gives
    structured accessibility/kid-friendly tags (wheelchair, changing table),
    Wikimedia gives richer narrative descriptions for well-known landmarks
    that OSM's `description` tag is often blank for.

    Returns a list of {title, summary_snippet, distance_m, latitude, longitude},
    ordered by distance (nearest first).
    """
    resp = requests.get(
        _ACTION_API_URL,
        params={
            "action": "query",
            "list": "geosearch",
            "gscoord": f"{latitude}|{longitude}",
            "gsradius": min(radius_m, 10000),  # 10km is the API's hard max
            "gslimit": limit,
            "format": "json",
        },
        timeout=10,
        headers=_REQUEST_HEADERS,
    )
    resp.raise_for_status()
    results = resp.json().get("query", {}).get("geosearch", [])
    return [
        {
            "title": r["title"],
            "distance_m": r.get("dist"),
            "latitude": r.get("lat"),
            "longitude": r.get("lon"),
        }
        for r in results
    ]
