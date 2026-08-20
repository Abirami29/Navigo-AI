# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Build the Vector Search index
# MAGIC
# MAGIC Embeds `destinations.wikimedia_summary` and a composite text field for each
# MAGIC `activities` row (description + accessibility/kid metadata folded in, per
# MAGIC docs/design.md section 5) so semantic search can retrieve on meaning —
# MAGIC "step-free playground with a changing table, quiet before 2pm" — not just
# MAGIC exact column filters.
# MAGIC
# MAGIC Hard filters (accessibility, age, diet) are applied BEFORE this semantic
# MAGIC step in navigo.agent.tools.search_eligible_activities — this notebook only
# MAGIC builds the index that step ranks against.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt databricks-vectorsearch

# COMMAND ----------

import os
import sys
sys.path.insert(0, "../src")

# Same two fixes already proven necessary in 01_ingest_seed_destinations.py
# and 03_decision_analytics_cdf.py — this notebook was missing BOTH (never
# actually run yet, so the gap was latent rather than already discovered):
# (1) psycopg needs to be forced to its pure-Python implementation, or a
#     pip-installed psycopg-binary alongside it causes a SIGABRT crash in
#     Databricks Runtime; (2) LAKEBASE_* credentials need to be injected
#     from secrets, since Jobs/notebooks don't read .env and nothing else
#     wires them in here. Both must happen before `from navigo.db import
#     client as db` below.
os.environ["PSYCOPG_IMPL"] = "python"

dbutils.widgets.text("lakebase_port", "5432", "Lakebase port")
dbutils.widgets.text("lakebase_db", "databricks_postgres", "Lakebase database name")

if not os.getenv("LAKEBASE_HOST"):
    os.environ["LAKEBASE_PORT"] = dbutils.widgets.get("lakebase_port")
    os.environ["LAKEBASE_DB"] = dbutils.widgets.get("lakebase_db")
    os.environ["LAKEBASE_HOST"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_host")
    os.environ["LAKEBASE_USER"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_user")
    os.environ["LAKEBASE_PASSWORD"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_password")

from databricks.vector_search.client import VectorSearchClient

from navigo.config import DATABRICKS
from navigo.db import client as db

CATALOG = "navigo"
SCHEMA = "default"
SOURCE_TABLE = f"{CATALOG}.{SCHEMA}.activities_search_source"
INDEX_NAME = DATABRICKS.vector_index
ENDPOINT_NAME = DATABRICKS.vector_search_endpoint

# Same fix needed here as in 03_decision_analytics_cdf.py — this notebook
# has never actually been run yet, so this gap was latent: nothing ever
# created the navigo.default catalog/schema this table writes into.
spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 1. Materialize a Delta table combining activity fields into one embeddable text column
# MAGIC
# MAGIC Lakebase is Postgres (OLTP); Vector Search indexes a Delta table (OLAP), so we
# MAGIC sync the relevant rows across. For a mockup, a batch pull + write is enough —
# MAGIC production would use Lakebase's built-in Delta sync.

# COMMAND ----------

rows = db.fetch_all(
    """
    SELECT activity_id, destination_id, name, category, description,
           osm_wheelchair, has_accessible_toilet, has_changing_table, has_highchairs,
           min_recommended_age, max_recommended_age, quiet_hours, dietary_tags
    FROM activities
    """
)

def to_embeddable_text(r: dict) -> str:
    parts = [r["name"], r["category"], r["description"] or ""]
    if r["osm_wheelchair"] == "yes":
        parts.append("wheelchair accessible")
    if r["has_changing_table"]:
        parts.append("has a baby changing table")
    if r["has_highchairs"]:
        parts.append("has highchairs")
    if r["min_recommended_age"] is not None:
        parts.append(f"suitable from age {r['min_recommended_age']}")
    if r["quiet_hours"]:
        parts.append(f"quiet hours: {r['quiet_hours']}")
    if r["dietary_tags"]:
        parts.append("dietary options: " + ", ".join(r["dietary_tags"]))
    return ". ".join(p for p in parts if p)

records = [
    {
        "activity_id": r["activity_id"],
        "destination_id": r["destination_id"],
        "text": to_embeddable_text(r),
    }
    for r in rows
]

df = spark.createDataFrame(records)
df.write.mode("overwrite").saveAsTable(SOURCE_TABLE)
spark.sql(f"ALTER TABLE {SOURCE_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print(f"Wrote {len(records)} rows to {SOURCE_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2. Create (or sync) the Vector Search index

# COMMAND ----------

vsc = VectorSearchClient()

if ENDPOINT_NAME not in [e["name"] for e in vsc.list_endpoints().get("endpoints", [])]:
    vsc.create_endpoint(name=ENDPOINT_NAME, endpoint_type="STANDARD")

existing_indexes = [i["name"] for i in vsc.list_indexes(ENDPOINT_NAME).get("vector_indexes", [])]
if INDEX_NAME in existing_indexes:
    vsc.get_index(ENDPOINT_NAME, INDEX_NAME).sync()
    print(f"Synced existing index {INDEX_NAME}")
else:
    vsc.create_delta_sync_index(
        endpoint_name=ENDPOINT_NAME,
        index_name=INDEX_NAME,
        source_table_name=SOURCE_TABLE,
        pipeline_type="TRIGGERED",
        primary_key="activity_id",
        embedding_source_column="text",
        embedding_model_endpoint_name="databricks-gte-large-en",
    )
    print(f"Created index {INDEX_NAME}")
