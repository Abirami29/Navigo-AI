# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Decision analytics via Change Data Feed
# MAGIC
# MAGIC Closes a real gap: `activities_search_source` (used by
# MAGIC `02_build_vector_index.py`) has CDF enabled, but it's fully
# MAGIC **overwritten** on every rebuild — CDF on an overwritten table just
# MAGIC reports "everything got deleted and reinserted" each time, not
# MAGIC meaningful business changes. This notebook uses a genuinely incremental
# MAGIC table instead: `agent_decisions` (Lakebase/Postgres) is synced
# MAGIC **append-only** into a Delta mirror, CDF is enabled on THAT table, and
# MAGIC only the actual new rows since the last run get read via CDF and
# MAGIC aggregated into an analytics table.
# MAGIC
# MAGIC Three tables, all in Unity Catalog:
# MAGIC   - `navigo.default.agent_decisions_mirror` — append-only Delta mirror
# MAGIC     of Lakebase's agent_decisions, CDF enabled
# MAGIC   - `navigo.default.agent_decisions_analytics` — daily counts by
# MAGIC     decision_type/trigger, built ONLY from CDF-reported inserts since
# MAGIC     the last run (not a full table rescan every time)
# MAGIC   - `navigo.default.cdf_checkpoints` — tracks (a) the latest
# MAGIC     agent_decisions.created_at already pulled from Lakebase, and (b)
# MAGIC     the latest Delta table version already processed via CDF — without
# MAGIC     this, every run would reprocess everything from scratch
# MAGIC
# MAGIC Runs as a scheduled batch job (see
# MAGIC resources/jobs/decision_analytics_job.yml) — deliberately NOT
# MAGIC Structured Streaming, since Free Edition is serverless/job-based, not
# MAGIC suited to an always-on stream.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

import os
import sys
sys.path.insert(0, "../src")

# Forces psycopg to use ONLY its pure-Python implementation — see
# 01_ingest_seed_destinations.py for the full reasoning. A real run of THIS
# notebook hit the exact SIGABRT crash this line exists to prevent: pip
# installed psycopg-binary alongside plain psycopg despite requirements.txt
# only asking for the latter, and psycopg's own import-time selection
# preferred the binary one anyway. Must be set before `from navigo.db
# import client as db` below.
os.environ["PSYCOPG_IMPL"] = "python"

dbutils.widgets.text("lakebase_port", "5432", "Lakebase port")
dbutils.widgets.text("lakebase_db", "databricks_postgres", "Lakebase database name")

if not os.getenv("LAKEBASE_HOST"):
    os.environ["LAKEBASE_PORT"] = dbutils.widgets.get("lakebase_port")
    os.environ["LAKEBASE_DB"] = dbutils.widgets.get("lakebase_db")
    # Same navigo_secrets scope used everywhere else in this project —
    # one shared source of truth for Lakebase credentials.
    os.environ["LAKEBASE_HOST"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_host")
    os.environ["LAKEBASE_USER"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_user")
    os.environ["LAKEBASE_PASSWORD"] = dbutils.secrets.get(scope="navigo_secrets", key="lakebase_password")

from navigo.db import client as db

CATALOG = "navigo"
SCHEMA = "default"
MIRROR_TABLE = f"{CATALOG}.{SCHEMA}.agent_decisions_mirror"
ANALYTICS_TABLE = f"{CATALOG}.{SCHEMA}.agent_decisions_analytics"
CHECKPOINT_TABLE = f"{CATALOG}.{SCHEMA}.cdf_checkpoints"

# Neither this notebook nor 02_build_vector_index.py ever creates the
# catalog/schema they both write into — a real run hit
# AnalysisException: [SCHEMA_NOT_FOUND] The schema `navigo.default` cannot
# be found, since nothing had ever actually created it (02 was also never
# run yet). IF NOT EXISTS makes this safe to run every time regardless of
# which notebook happens to run first.
spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 0. Checkpoint table — tracks progress across runs
# MAGIC
# MAGIC Without this, every run would either reprocess everything (wasteful,
# MAGIC and would double-count analytics) or need to guess where it left off.

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} (
        checkpoint_key STRING,
        checkpoint_value STRING
    ) USING DELTA
""")


def get_checkpoint(key: str, default: str) -> str:
    row = spark.sql(
        f"SELECT checkpoint_value FROM {CHECKPOINT_TABLE} WHERE checkpoint_key = '{key}'"
    ).collect()
    return row[0]["checkpoint_value"] if row else default


def set_checkpoint(key: str, value: str) -> None:
    spark.sql(f"DELETE FROM {CHECKPOINT_TABLE} WHERE checkpoint_key = '{key}'")
    spark.createDataFrame([(key, value)], ["checkpoint_key", "checkpoint_value"]) \
        .write.mode("append").saveAsTable(CHECKPOINT_TABLE)


# COMMAND ----------

# MAGIC %md
# MAGIC ### 1. Incremental sync: Lakebase -> Delta mirror (APPEND only)
# MAGIC
# MAGIC Only pulls rows created after the last synced timestamp — this is
# MAGIC what makes CDF on the mirror meaningful. An overwrite here would
# MAGIC reproduce the exact "CDF just reports full-table churn" problem this
# MAGIC whole notebook exists to avoid.

# COMMAND ----------

last_synced_at = get_checkpoint("agent_decisions_last_synced_at", "1970-01-01T00:00:00")

new_rows = db.fetch_all(
    """
    SELECT decision_id, trip_id, item_id, decision_type, trigger, explanation, created_at
    FROM agent_decisions
    WHERE created_at > %s
    ORDER BY created_at
    """,
    (last_synced_at,),
)

print(f"Found {len(new_rows)} new decision(s) since {last_synced_at}")

if new_rows:
    records = [
        {
            "decision_id": str(r["decision_id"]),
            "trip_id": str(r["trip_id"]),
            "item_id": str(r["item_id"]) if r["item_id"] else None,
            "decision_type": r["decision_type"],
            "trigger": r["trigger"],
            "explanation": r["explanation"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in new_rows
    ]
    df = spark.createDataFrame(records)

    # CDF must be enabled AT CREATION, not via a later ALTER TABLE — a real
    # run proved this the hard way: enabling it after the fact meant the
    # very first batch of inserts (written in the CREATE/append itself)
    # predated CDF being active, so a subsequent CDF read starting at
    # version 0 found nothing to report at all. Creating the table
    # explicitly with the property set means CDF is active from version 0
    # onward, covering the first write too.
    if not spark.catalog.tableExists(MIRROR_TABLE):
        spark.sql(f"""
            CREATE TABLE {MIRROR_TABLE} (
                decision_id STRING, trip_id STRING, item_id STRING,
                decision_type STRING, trigger STRING, explanation STRING,
                created_at STRING
            ) USING DELTA TBLPROPERTIES (delta.enableChangeDataFeed = true)
        """)
    df.write.mode("append").option("mergeSchema", "true").saveAsTable(MIRROR_TABLE)

    newest_created_at = max(r["created_at"] for r in new_rows).isoformat()
    set_checkpoint("agent_decisions_last_synced_at", newest_created_at)
    print(f"Synced. New checkpoint: {newest_created_at}")
else:
    print("Nothing new to sync.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2. Read CDF changes and aggregate into analytics
# MAGIC
# MAGIC Batch read (not Structured Streaming) — fits a scheduled job cleanly,
# MAGIC no always-on cluster needed, consistent with how every other job in
# MAGIC this project runs. Aggregation into daily counts by
# MAGIC decision_type/trigger happens in the same cell as the CDF read below
# MAGIC (not split into a separate cell) — see the comment in that cell for why.

# COMMAND ----------

table_exists = spark.catalog.tableExists(MIRROR_TABLE)

if table_exists:
    current_version = spark.sql(f"DESCRIBE HISTORY {MIRROR_TABLE} LIMIT 1").collect()[0]["version"]
    last_processed_version = int(get_checkpoint("mirror_last_processed_version", "-1"))

    if current_version > last_processed_version:
        changes_df = (
            spark.read.format("delta")
            .option("readChangeFeed", "true")
            .option("startingVersion", last_processed_version + 1)
            .table(MIRROR_TABLE)
        )
        # Only care about genuinely new rows here — this mirror is
        # append-only, so update_preimage/update_postimage/delete should
        # never actually occur, but filtering defensively rather than
        # assuming that invariant always holds.
        inserts_df = changes_df.filter(changes_df["_change_type"] == "insert")

        # Step 3: aggregate into the analytics table — daily counts by
        # decision_type/trigger, the specific "usage stats" shape suggested
        # by the external review, built exclusively from what CDF reported
        # as new, not a full table rescan.
        #
        # NOTE: deliberately NOT split into a separate notebook cell here
        # (no "# COMMAND ----------" marker) — a real run hit
        # "IndentationError: unexpected indent" because this code sits
        # inside the `if current_version > last_processed_version:` block
        # above. Databricks executes each cell as an independent unit, so
        # a cell boundary inside a Python control-flow block is invalid;
        # an `else:` (or any indented continuation) with no matching `if:`
        # in the SAME cell can't parse. Everything inside this nested
        # if/else must stay in one cell.
        from pyspark.sql import functions as F

        daily_counts = (
            inserts_df
            .withColumn("decision_date", F.to_date("created_at"))
            .groupBy("decision_date", "decision_type", "trigger")
            .count()
            .withColumnRenamed("count", "decision_count")
        )

        n_new = daily_counts.count()
        if n_new > 0:
            spark.sql(f"""
                CREATE TABLE IF NOT EXISTS {ANALYTICS_TABLE} (
                    decision_date DATE,
                    decision_type STRING,
                    trigger STRING,
                    decision_count BIGINT
                ) USING DELTA
            """)
            daily_counts.createOrReplaceTempView("new_counts")
            # MERGE rather than append — the same (date, decision_type,
            # trigger) combination can appear across multiple runs on the
            # same day, and should accumulate, not duplicate as separate rows.
            spark.sql(f"""
                MERGE INTO {ANALYTICS_TABLE} AS target
                USING new_counts AS source
                ON target.decision_date = source.decision_date
                   AND target.decision_type = source.decision_type
                   AND target.trigger = source.trigger
                WHEN MATCHED THEN UPDATE SET
                    target.decision_count = target.decision_count + source.decision_count
                WHEN NOT MATCHED THEN INSERT *
            """)
            print(f"Merged {n_new} new (date, decision_type, trigger) group(s) into analytics.")
        else:
            print("No new inserts found via CDF this run.")

        set_checkpoint("mirror_last_processed_version", str(current_version))
    else:
        print("No new Delta versions to process.")
else:
    print("Mirror table doesn't exist yet — nothing to read via CDF this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Sanity check: what's actually in the analytics table now

# COMMAND ----------

if spark.catalog.tableExists(ANALYTICS_TABLE):
    display(spark.sql(f"""
        SELECT decision_date, decision_type, trigger, decision_count
        FROM {ANALYTICS_TABLE}
        ORDER BY decision_date DESC, decision_count DESC
    """))
