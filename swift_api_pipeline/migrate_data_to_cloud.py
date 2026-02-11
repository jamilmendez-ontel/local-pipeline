"""
migrate_data_to_cloud.py -- Copy all data from local Supabase -> cloud Supabase.

Uses asyncpg COPY protocol (binary format) for maximum throughput.
All tables stream through temp files on disk to avoid OOM with large JSONB tables.

Usage:
    python migrate_data_to_cloud.py

Requires:
    - Local Supabase running on port 54322
    - Cloud Supabase DB accessible
    - asyncpg installed
"""

import asyncio
import os
import sys
import tempfile
import time

import asyncpg

# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

LOCAL_DSN = "postgresql://supabase_admin:postgres@127.0.0.1:54322/postgres"

CLOUD_DSN = (
    "postgresql://postgres:[REDACTED-OLD-PW]"
    "@db.voqfjfngdpcvevbkikud.supabase.co:5432/postgres"
)

# ---------------------------------------------------------------------------
# Table ordering (FK-safe: referenced tables first)
# ---------------------------------------------------------------------------

TABLES: list[tuple[str, str]] = [
    # pipeline schema
    ("pipeline", "pipeline_runs"),
    ("pipeline", "requirements_extraction_progress"),

    # data_raw schema
    ("data_raw", "raw_organizations"),
    ("data_raw", "raw_projects"),
    ("data_raw", "raw_user_priorities"),
    ("data_raw", "raw_asset_tasks"),
    ("data_raw", "raw_asset_task_requirements"),
    ("data_raw", "raw_form_qa_ts13"),
    ("data_raw", "raw_form_qa_ts14"),
    ("data_raw", "raw_form_qa_ts15"),
    ("data_raw", "raw_form_qa_ts16"),
    ("data_raw", "raw_form_qa_ts17"),
    ("data_raw", "raw_form_qa_ts18"),
    ("data_raw", "raw_timer_activities"),
    ("data_raw", "raw_timer_activities_historical"),
    ("data_raw", "raw_ar_aging"),
    ("data_raw", "raw_sales_detail"),

    # data_staging schema
    ("data_staging", "stg_organizations"),
    ("data_staging", "stg_projects"),
    ("data_staging", "stg_user_priorities"),
    ("data_staging", "stg_assets"),
    ("data_staging", "stg_asset_tasks"),
    ("data_staging", "stg_asset_task_requirements"),
    ("data_staging", "stg_qa_form"),
    ("data_staging", "stg_timer_activities"),
    ("data_staging", "stg_ar_aging"),
    ("data_staging", "stg_sales_detail"),

    # agent schema
    ("agent", "schema_metadata"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_rows(n: int) -> str:
    return f"{n:,}"


def fmt_time(seconds: float) -> str:
    if seconds >= 60:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"


async def get_row_count(conn: asyncpg.Connection, schema: str, table: str) -> int:
    row = await conn.fetchrow(
        f'SELECT count(*) AS cnt FROM "{schema}"."{table}"'
    )
    return row["cnt"]


async def table_exists(conn: asyncpg.Connection, schema: str, table: str) -> bool:
    row = await conn.fetchrow(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables"
        "  WHERE table_schema = $1 AND table_name = $2"
        ") AS ok",
        schema, table,
    )
    return row["ok"]


async def get_columns(conn: asyncpg.Connection, schema: str, table: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2 "
        "ORDER BY ordinal_position",
        schema, table,
    )
    return [r["column_name"] for r in rows]


# ---------------------------------------------------------------------------
# Core copy logic -- all via temp file
# ---------------------------------------------------------------------------

async def copy_table(
    local: asyncpg.Connection,
    cloud: asyncpg.Connection,
    schema: str,
    table: str,
    columns: list[str],
) -> int:
    """Copy a table from local to cloud via temp file + binary COPY."""
    qualified = f'"{schema}"."{table}"'

    # Create temp file
    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=".pgcopy", prefix=f"{schema}_{table}_"
    )
    tmp_path = tmp.name
    tmp.close()

    try:
        # Export from local -> temp file
        await local.copy_from_query(
            f"SELECT * FROM {qualified}",
            output=tmp_path,
            format="binary",
        )

        file_size = os.path.getsize(tmp_path)
        print(f"  ({file_size / (1024*1024):.1f} MB)", end="", flush=True)

        # Truncate cloud table
        await cloud.execute(f"TRUNCATE {qualified} CASCADE")

        # Import from temp file -> cloud
        await cloud.copy_to_table(
            table,
            schema_name=schema,
            source=tmp_path,
            format="binary",
            columns=columns,
        )

        return await get_row_count(cloud, schema, table)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

async def migrate_table(
    local: asyncpg.Connection,
    cloud: asyncpg.Connection,
    schema: str,
    table: str,
    index: int,
    total: int,
) -> dict:
    """Migrate a single table and return result info."""
    qualified = f"{schema}.{table}"
    prefix = f"  [{index}/{total}]"

    # Check source exists
    if not await table_exists(local, schema, table):
        print(f"{prefix} SKIP  {qualified}  (not found in local)")
        return {"table": qualified, "status": "skip", "local": 0, "cloud": 0, "time": 0}

    # Check cloud target exists
    if not await table_exists(cloud, schema, table):
        print(f"{prefix} SKIP  {qualified}  (not found in cloud)")
        return {"table": qualified, "status": "skip", "local": 0, "cloud": 0, "time": 0}

    # Get row count
    local_count = await get_row_count(local, schema, table)
    if local_count == 0:
        print(f"{prefix} SKIP  {qualified}  (0 rows)")
        return {"table": qualified, "status": "skip", "local": 0, "cloud": 0, "time": 0}

    print(f"{prefix} COPY  {qualified}  ({fmt_rows(local_count)} rows) ...", end="", flush=True)
    t0 = time.perf_counter()

    columns = await get_columns(local, schema, table)

    try:
        cloud_count = await copy_table(local, cloud, schema, table, columns)
        elapsed = time.perf_counter() - t0
        match = "OK" if cloud_count == local_count else "MISMATCH"
        print(f" {match}  ({fmt_time(elapsed)})")

        return {
            "table": qualified,
            "status": "ok" if match == "OK" else "mismatch",
            "local": local_count,
            "cloud": cloud_count,
            "time": elapsed,
        }

    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f" FAIL  ({fmt_time(elapsed)})")
        print(f"         Error: {e}")
        return {
            "table": qualified,
            "status": "fail",
            "local": local_count,
            "cloud": 0,
            "time": elapsed,
            "error": str(e),
        }


async def main() -> None:
    total = len(TABLES)

    print(f"\n{'='*65}")
    print(f"  Data Migration: Local Supabase -> Cloud Supabase")
    print(f"  Tables: {total}")
    print(f"{'='*65}\n")

    # Connect to both databases
    print("  Connecting to local DB ...", end="", flush=True)
    local = await asyncpg.connect(LOCAL_DSN)
    print(" OK")

    print("  Connecting to cloud DB ...", end="", flush=True)
    cloud = await asyncpg.connect(CLOUD_DSN)
    print(" OK")

    # Disable statement timeout on cloud for large COPY operations
    await cloud.execute("SET statement_timeout = '0'")
    print("  Cloud statement_timeout disabled for migration\n")

    results: list[dict] = []

    for i, (schema, table) in enumerate(TABLES, 1):
        result = await migrate_table(local, cloud, schema, table, i, total)
        results.append(result)

    await local.close()
    await cloud.close()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print(f"\n{'='*65}")
    print(f"  Migration Summary")
    print(f"{'='*65}")
    print(f"  {'Table':<45} {'Local':>10} {'Cloud':>10} {'Status':>8}")
    print(f"  {'-'*45} {'-'*10} {'-'*10} {'-'*8}")

    total_local = 0
    total_cloud = 0
    ok_count = 0
    fail_count = 0
    skip_count = 0

    for r in results:
        status_label = r["status"].upper()
        print(
            f"  {r['table']:<45} {fmt_rows(r['local']):>10} {fmt_rows(r['cloud']):>10} {status_label:>8}"
        )
        total_local += r["local"]
        total_cloud += r["cloud"]
        if r["status"] == "ok":
            ok_count += 1
        elif r["status"] == "fail" or r["status"] == "mismatch":
            fail_count += 1
        else:
            skip_count += 1

    print(f"  {'-'*45} {'-'*10} {'-'*10} {'-'*8}")
    print(f"  {'TOTAL':<45} {fmt_rows(total_local):>10} {fmt_rows(total_cloud):>10}")
    print()
    print(f"  OK: {ok_count}  |  Failed: {fail_count}  |  Skipped: {skip_count}")

    total_time = sum(r["time"] for r in results)
    print(f"  Total time: {fmt_time(total_time)}")
    print(f"{'='*65}\n")

    if fail_count > 0:
        print("  *** SOME TABLES FAILED -- check errors above ***\n")
        sys.exit(1)
    else:
        print("  All tables migrated successfully!\n")


if __name__ == "__main__":
    asyncio.run(main())
