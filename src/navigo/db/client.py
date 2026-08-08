"""Thin Lakebase (Postgres) client for Navigo.

Uses psycopg 3. Connection details come from navigo.config.LAKEBASE, which
reads from env vars locally or from the Lakebase secret injected into the
Databricks App at runtime.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from navigo.config import LAKEBASE

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(LAKEBASE.dsn, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_schema() -> None:
    """Applies schema.sql — idempotent, safe to run repeatedly (uses IF NOT EXISTS)."""
    sql = SCHEMA_PATH.read_text()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


def execute_returning_id(query: str, params: tuple[Any, ...], id_column: str) -> Any:
    """For INSERT ... RETURNING <id_column> statements."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return row[id_column] if row else None
