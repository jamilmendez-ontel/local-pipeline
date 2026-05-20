"""Apply migration 052: Partition raw_asset_tasks by project_did.

Runs locally via asyncpg (no HTTP timeout, unlike Supabase MCP).
The migration moves ~2.5M rows and creates 8 partitions + 16 indexes,
which typically takes 2-5 min — too long for an HTTP-based migration tool.

Pre-flight: snapshot row counts and verify table is unpartitioned.
Apply: execute the SQL file (single transaction, atomic).
Post-verify: confirm partitions exist + row counts preserved per partition.
"""
import asyncio
import ssl
import sys
from pathlib import Path
from dotenv import load_dotenv
import os

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def main():
    import asyncpg

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Direct DB connection (not the pooler) — long-running DDL + bulk INSERT
    # needs an unbounded session.
    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl=ctx,
        # Critical: disable statement timeout for this connection so the
        # 2.5M-row INSERT-SELECT can run to completion.
        server_settings={"statement_timeout": "0"},
    )

    sql_path = Path(__file__).with_name("052_partition_raw_asset_tasks.sql")
    sql = sql_path.read_text(encoding="utf-8")

    # ─── Pre-flight ─────────────────────────────────────────────────────────
    print("Migration 052: Partition raw_asset_tasks by project_did")
    print("=" * 70)

    is_partitioned = await conn.fetchval(
        "SELECT c.relkind = 'p' FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname = 'data_raw' AND c.relname = 'raw_asset_tasks'"
    )
    if is_partitioned:
        print("ERROR: raw_asset_tasks is already partitioned. Migration already applied?")
        sys.exit(1)

    rows_before = await conn.fetchval("SELECT COUNT(*) FROM data_raw.raw_asset_tasks")
    project_dids_before = await conn.fetchval(
        "SELECT COUNT(DISTINCT project_did) FROM data_raw.raw_asset_tasks"
    )
    print(f"Pre-flight:")
    print(f"  Table kind:      regular (unpartitioned)")
    print(f"  Total rows:      {rows_before:,}")
    print(f"  Distinct dids:   {project_dids_before}")

    # ─── Apply ──────────────────────────────────────────────────────────────
    print()
    print("Applying migration (expect 2-5 min for the data copy)...")
    await conn.execute(sql)
    print("Migration applied successfully.")

    # ─── Post-verify ────────────────────────────────────────────────────────
    print()
    print("Post-verify:")
    is_partitioned_after = await conn.fetchval(
        "SELECT c.relkind = 'p' FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname = 'data_raw' AND c.relname = 'raw_asset_tasks'"
    )
    if not is_partitioned_after:
        print("FAIL: raw_asset_tasks is NOT partitioned after migration.")
        sys.exit(1)
    print(f"  Table kind:      partitioned OK")

    rows_after = await conn.fetchval("SELECT COUNT(*) FROM data_raw.raw_asset_tasks")
    if rows_after != rows_before:
        print(f"FAIL: row count mismatch (before={rows_before:,}, after={rows_after:,})")
        sys.exit(1)
    print(f"  Total rows:      {rows_after:,} OK (matches pre-flight)")

    # Per-partition row counts
    print()
    print("  Per-partition row counts:")
    partition_rows = await conn.fetch(
        "SELECT tableoid::regclass::text AS partition, COUNT(*) AS n "
        "FROM data_raw.raw_asset_tasks "
        "GROUP BY tableoid::regclass ORDER BY 1"
    )
    for row in partition_rows:
        print(f"    {row['partition']:50s} {row['n']:>10,}")

    # Confirm old table is gone
    old_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname = 'data_raw' AND c.relname = 'raw_asset_tasks_old')"
    )
    if old_exists:
        print()
        print("WARN: raw_asset_tasks_old still exists (should be dropped).")
    else:
        print()
        print("  Old table dropped OK")

    await conn.close()
    print()
    print("=" * 70)
    print("Migration 052 complete.")


if __name__ == "__main__":
    asyncio.run(main())
