"""Central configuration for Navigo, loaded from environment variables / .env.

In Databricks, these are supplied via workspace secrets and app resources
(see resources/apps/navigo_app.yml) rather than a .env file.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _normalize_host(raw: str) -> str:
    """Ensures a host value has a URL scheme.

    Local .env has always had DATABRICKS_HOST as a full URL
    (https://dbc-...cloud.databricks.com). A real deployed Databricks App
    run showed the platform's own auto-injected DATABRICKS_HOST is a bare
    hostname instead (no scheme) — requests.post() then fails with
    MissingSchema trying to build a URL from it. Normalizing once here
    means every DATABRICKS.host consumer (agent.py's Model Serving calls,
    retrieval.py's Vector Search client) gets a correct URL either way,
    rather than needing the same defensive check in multiple places.
    """
    if not raw or raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://{raw}"


@dataclass(frozen=True)
class LakebaseConfig:
    host: str = os.getenv("LAKEBASE_HOST", "")
    port: int = int(os.getenv("LAKEBASE_PORT", "5432"))
    database: str = os.getenv("LAKEBASE_DB", "navigo")
    user: str = os.getenv("LAKEBASE_USER", "")
    password: str = os.getenv("LAKEBASE_PASSWORD", "")

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password} sslmode=require"
        )


@dataclass(frozen=True)
class DatabricksConfig:
    host: str = _normalize_host(os.getenv("DATABRICKS_HOST", ""))
    token: str = os.getenv("DATABRICKS_TOKEN", "")
    model_serving_endpoint: str = os.getenv(
        "NAVIGO_MODEL_SERVING_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct"
    )
    vector_search_endpoint: str = os.getenv("NAVIGO_VECTOR_SEARCH_ENDPOINT", "navigo_vector_endpoint")
    vector_index: str = os.getenv("NAVIGO_VECTOR_INDEX", "navigo.default.activities_index")


@dataclass(frozen=True)
class ExternalApiConfig:
    open_meteo_base_url: str = os.getenv("OPEN_METEO_BASE_URL", "https://api.open-meteo.com")
    open_meteo_geocoding_url: str = os.getenv(
        "OPEN_METEO_GEOCODING_URL", "https://geocoding-api.open-meteo.com"
    )
    open_meteo_air_quality_url: str = os.getenv(
        "OPEN_METEO_AIR_QUALITY_URL", "https://air-quality-api.open-meteo.com"
    )
    wikimedia_base_url: str = os.getenv("WIKIMEDIA_BASE_URL", "https://en.wikipedia.org/api/rest_v1")
    overpass_api_url: str = os.getenv("OVERPASS_API_URL", "https://overpass.kumi.systems/api/interpreter")


LAKEBASE = LakebaseConfig()
DATABRICKS = DatabricksConfig()
EXTERNAL_APIS = ExternalApiConfig()
