"""
migrate_cloud.py — Run all pipeline + backend migrations against cloud Supabase.

Usage:
    python migrate_cloud.py --db-url "postgresql://postgres.[ref]:[pass]@aws-0-[region].pooler.supabase.com:5432/postgres"

Runs each migration in its own transaction, prints progress per file.
Skips deprecated files: 002_staging_views.sql, 008_create_schemas.sql
"""

import asyncio
import argparse
import sys
import time
from pathlib import Path

import asyncpg


# --------------------------------------------------------------------------- #
# Migration ordering
# --------------------------------------------------------------------------- #

PIPELINE_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BACKEND_MIGRATIONS_DIR = (
    Path(__file__).parent.parent.parent / "local-ai-agent" / "backend" / "migrations"
)

SKIP_FILES = {
    "002_staging_views.sql",
    "008_create_schemas.sql",
}

# Pre-setup SQL: create schemas that 008_v2 and later migrations expect
PRE_SETUP_SQL = """
CREATE SCHEMA IF NOT EXISTS pipeline;
CREATE SCHEMA IF NOT EXISTS reference;
"""

# Post-008 SQL: move pipeline_runs into the pipeline schema
POST_008_SQL = """
ALTER TABLE public.pipeline_runs SET SCHEMA pipeline;

-- Grant permissions on pipeline schema tables (now that pipeline_runs is there)
GRANT SELECT ON ALL TABLES IN SCHEMA pipeline TO anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA pipeline TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA pipeline TO service_role;
"""

# Ordered list of (label, source) tuples
# source is either a Path to a .sql file or a raw SQL string
def build_migration_plan() -> list[tuple[str, str | Path]]:
    plan: list[tuple[str, str | Path]] = []

    # 0. Pre-setup: create pipeline + reference schemas
    plan.append(("pre-setup: create pipeline & reference schemas", PRE_SETUP_SQL))

    # Pipeline migrations in order
    pipeline_order = [
        "001_raw_tables.sql",
        "002_staging_tables.sql",
        "003_asset_tasks_tables.sql",
        "004_forms_tables.sql",
        "005_forms_ts18.sql",
        "006_stg_qa_form_all_columns.sql",
        "007_timer_tables.sql",
        "008_create_schemas_v2.sql",
    ]

    for fname in pipeline_order:
        plan.append((fname, PIPELINE_MIGRATIONS_DIR / fname))

    # Post-008: move pipeline_runs
    plan.append(("post-008: move pipeline_runs to pipeline schema", POST_008_SQL))

    pipeline_order_2 = [
        "009_requirements_tables.sql",
        "010_assets_table.sql",
        "011_timer_staging_dates.sql",
        "012_task_name_clean.sql",
        "013_task_name_clean_all_tables.sql",
        "014_aggregate_assets_rpc.sql",
        "015_raw_timer_historical.sql",
        "016_ar_aging_tables.sql",
        "017_sales_detail_tables.sql",
    ]

    for fname in pipeline_order_2:
        plan.append((fname, PIPELINE_MIGRATIONS_DIR / fname))

    # Backend migrations
    backend_order = [
        "009_schema_metadata.sql",
        "010_indexes_and_metadata.sql",
    ]

    for fname in backend_order:
        plan.append((fname, BACKEND_MIGRATIONS_DIR / fname))

    return plan


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

async def run_migration(conn: asyncpg.Connection, label: str, sql: str) -> None:
    """Execute a single migration inside a transaction."""
    async with conn.transaction():
        await conn.execute(sql)


async def main(db_url: str) -> None:
    plan = build_migration_plan()
    total = len(plan)

    print(f"\n{'='*60}")
    print(f"  Cloud Migration Runner — {total} steps")
    print(f"{'='*60}\n")

    conn = await asyncpg.connect(db_url)
    print(f"Connected to: {db_url.split('@')[1] if '@' in db_url else db_url}\n")

    passed = 0
    failed = 0

    for i, (label, source) in enumerate(plan, 1):
        # Load SQL
        if isinstance(source, Path):
            if not source.exists():
                print(f"  [{i}/{total}] SKIP  {label}  (file not found: {source.name})")
                failed += 1
                continue
            sql = source.read_text(encoding="utf-8")
        else:
            sql = source

        t0 = time.perf_counter()
        try:
            await run_migration(conn, label, sql)
            elapsed = time.perf_counter() - t0
            print(f"  [{i}/{total}] OK    {label}  ({elapsed:.1f}s)")
            passed += 1
        except Exception as e:
            elapsed = time.perf_counter() - t0
            print(f"  [{i}/{total}] FAIL  {label}  ({elapsed:.1f}s)")
            print(f"           Error: {e}")
            failed += 1

    await conn.close()

    print(f"\n{'='*60}")
    print(f"  Done: {passed} passed, {failed} failed (out of {total})")
    print(f"{'='*60}\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run migrations against cloud Supabase")
    parser.add_argument(
        "--db-url",
        required=True,
        help='PostgreSQL connection string, e.g. "postgresql://postgres.[ref]:[pass]@aws-0-us-east-1.pooler.supabase.com:5432/postgres"',
    )
    args = parser.parse_args()
    asyncio.run(main(args.db_url))
