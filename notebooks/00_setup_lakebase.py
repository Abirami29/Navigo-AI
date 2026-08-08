# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Set up Lakebase schema
# MAGIC Run this once against a fresh Lakebase project to create Navigo's tables.
# MAGIC Safe to re-run — schema.sql uses `CREATE TABLE IF NOT EXISTS`.

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

import sys
sys.path.insert(0, "../src")

from navigo.db.client import apply_schema

apply_schema()
print("Navigo schema applied.")

# COMMAND ----------

# MAGIC %md
# MAGIC Sanity check: list the tables that now exist.

# COMMAND ----------

from navigo.db.client import fetch_all

tables = fetch_all(
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
)
for t in tables:
    print(t["table_name"])
