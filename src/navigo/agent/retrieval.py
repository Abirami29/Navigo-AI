"""Queries the Databricks Vector Search index built by
notebooks/02_build_vector_index.py — this is the piece that was missing
entirely before: the index existed, but nothing ever queried it.

Unlike building/syncing the index (which needs Spark and only runs inside a
Databricks notebook), *querying* an already-built index works from any
Python process — a local terminal, the agent running locally, or the
deployed Databricks App — via VectorSearchClient authenticated with a
workspace host + token. That's what makes this importable from
navigo.agent.tools without requiring a notebook environment.
"""

from __future__ import annotations

from navigo.config import DATABRICKS

_RETURN_COLUMNS = ["activity_id", "destination_id"]


def semantic_search_activities(query_text: str, destination_id: str, top_k: int = 15) -> list[str]:
    """Searches the activities vector index for the best semantic matches to
    `query_text`, restricted to one destination. Returns a list of
    activity_ids ranked by relevance (best first) — callers (see
    navigo.agent.tools.search_activities_by_interest) look these rows up in
    Lakebase and apply hard accessibility/diet/age filters afterward. This
    function only does the "retrieve based on interests" half of context
    engineering; it deliberately knows nothing about accessibility or diet.

    Returns an empty list (not an exception) if the index/endpoint isn't
    reachable — semantic search failing should degrade the agent to hard
    filtering only, not crash the whole itinerary-generation flow. Errors are
    only swallowed here; callers that want to know why should catch
    VectorSearchClient errors themselves if they need to.
    """
    try:
        from databricks.vector_search.client import VectorSearchClient
    except ImportError:
        # databricks-vectorsearch isn't installed (e.g. a minimal local
        # environment) — degrade gracefully rather than crash the caller.
        return []

    try:
        # No explicit workspace_url/personal_access_token: those force a
        # PAT-only assumption that broke inside a deployed Databricks App
        # (no DATABRICKS_TOKEN exists there — see agent.py's
        # _get_auth_headers for the same issue, hit for real on a live
        # deploy). Constructed bare, VectorSearchClient auto-resolves
        # credentials the same unified way WorkspaceClient does — the
        # app's own service-principal OAuth when deployed, your PAT locally.
        vsc = VectorSearchClient()
        index = vsc.get_index(DATABRICKS.vector_search_endpoint, DATABRICKS.vector_index)
        results = index.similarity_search(
            query_text=query_text,
            columns=_RETURN_COLUMNS,
            filters={"destination_id": destination_id},
            num_results=top_k,
        )
        rows = results.get("result", {}).get("data_array", [])
        # Row shape is [activity_id, destination_id, score] in the same order
        # as _RETURN_COLUMNS plus a trailing similarity score.
        return [row[0] for row in rows]
    except Exception:
        return []
