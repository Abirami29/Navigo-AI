"""Wikimedia (Wikipedia REST API) client for destination and attraction summaries.

Used to give the agent narrative context for the "why we picked this" text,
and as embedding input for semantic retrieval. Docs:
https://en.wikipedia.org/api/rest_v1/
"""

from __future__ import annotations

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from navigo.config import EXTERNAL_APIS

_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))


@_RETRY
def get_summary(title: str) -> str | None:
    """Returns a short plain-text summary for a place title, or None if not found."""
    resp = requests.get(
        f"{EXTERNAL_APIS.wikimedia_base_url}/page/summary/{requests.utils.quote(title)}",
        timeout=10,
        headers={"User-Agent": "navigo-ai/0.1 (family holiday planner demo)"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return data.get("extract")
