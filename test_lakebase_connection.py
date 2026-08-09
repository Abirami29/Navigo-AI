"""Test Lakebase connection using Databricks secrets."""

import os
import sys
from databricks.sdk import WorkspaceClient

# Get the connection string from Databricks secrets
w = WorkspaceClient()
connection_string = w.dbutils.secrets.get(scope="navigo_secrets", key="lakebase_connection")

print("✓ Retrieved secret from Databricks")
print(f"Connection string format: {connection_string[:30]}... (truncated)")

# Parse the connection string and set environment variables
# Expected format: postgresql://user:password@host:port/database
try:
    import psycopg
    from urllib.parse import urlparse
    
    parsed = urlparse(connection_string)
    os.environ["LAKEBASE_HOST"] = parsed.hostname or ""
    os.environ["LAKEBASE_PORT"] = str(parsed.port or 5432)
    os.environ["LAKEBASE_DB"] = parsed.path.lstrip("/") if parsed.path else "navigo"
    os.environ["LAKEBASE_USER"] = parsed.username or ""
    os.environ["LAKEBASE_PASSWORD"] = parsed.password or ""
    
    print("✓ Parsed connection parameters")
    print(f"  Host: {os.environ['LAKEBASE_HOST']}")
    print(f"  Port: {os.environ['LAKEBASE_PORT']}")
    print(f"  Database: {os.environ['LAKEBASE_DB']}")
    print(f"  User: {os.environ['LAKEBASE_USER']}")
    
    # Test the connection
    from navigo.db.client import get_connection
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            result = cur.fetchone()
            print(f"\n✓ Successfully connected to Lakebase!")
            print(f"  PostgreSQL version: {result['version']}")
    
    print("\n✅ All tests passed! Your Lakebase connection is working.")
    
except Exception as e:
    print(f"\n❌ Connection failed: {e}")
    sys.exit(1)
