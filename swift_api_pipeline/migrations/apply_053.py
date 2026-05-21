"""Apply migration 053: GC asset_tasks tables, RPC, and MVs.

Purely additive — no existing objects are touched. Safe during business hours.
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def main():
    import asyncpg

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl=ctx,
        server_settings={"statement_timeout": "0"},
    )

    sql_path = Path(__file__).with_name("053_asset_tasks_gc_tables.sql")
    sql = sql_path.read_text(encoding="utf-8")

    print("Migration 053: GC asset_tasks tables, RPC, and MVs")
    print("=" * 70)

    # Pre-flight: confirm the _gc objects don't exist yet
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname='data_raw' AND c.relname='raw_asset_tasks_gc')"
    )
    if exists:
        print("ERROR: raw_asset_tasks_gc already exists. Migration already applied?")
        sys.exit(1)
    print("Pre-flight: clean state (no _gc objects exist)")
    print()

    print("Applying migration...")
    await conn.execute(sql)
    print("Migration applied successfully.")
    print()

    # Post-verify
    print("Post-verify:")
    checks = [
        ("data_raw.raw_asset_tasks_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_raw' AND c.relname='raw_asset_tasks_gc')"),
        ("data_staging.stg_asset_tasks_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_staging' AND c.relname='stg_asset_tasks_gc')"),
        ("data_staging.stg_assets_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_staging' AND c.relname='stg_assets_gc')"),
        ("data_raw.aggregate_assets_gc function",
         "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid "
         "WHERE n.nspname='data_raw' AND p.proname='aggregate_assets_gc')"),
        ("analytics.mv_project_summary_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_project_summary_gc')"),
        ("analytics.mv_technician_stats_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_technician_stats_gc')"),
        ("analytics.mv_daily_completion_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_daily_completion_gc')"),
    ]
    all_ok = True
    for name, query in checks:
        ok = await conn.fetchval(query)
        marker = "OK" if ok else "MISSING"
        print(f"  {marker:8s} {name}")
        if not ok:
            all_ok = False

    # 3 indexes on raw + unique idx per MV
    idx_count = await conn.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE schemaname='data_raw' AND tablename='raw_asset_tasks_gc'"
    )
    print(f"  {'OK' if idx_count >= 3 else 'MISSING':8s} 3+ indexes on raw_asset_tasks_gc (found {idx_count})")

    mv_idx_count = await conn.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE schemaname='analytics' AND tablename LIKE 'mv_%_gc'"
    )
    print(f"  {'OK' if mv_idx_count >= 3 else 'MISSING':8s} unique idx per _gc MV (found {mv_idx_count})")

    # Confirm Ontel tables are untouched
    ontel_raw_partitioned = await conn.fetchval(
        "SELECT c.relkind = 'p' FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
        "WHERE n.nspname='data_raw' AND c.relname='raw_asset_tasks'"
    )
    print(f"  {'OK' if ontel_raw_partitioned else 'BAD':8s} Ontel raw_asset_tasks still partitioned (untouched)")

    await conn.close()

    print()
    print("=" * 70)
    if all_ok and idx_count >= 3 and mv_idx_count >= 3 and ontel_raw_partitioned:
        print("Migration 053 complete.")
    else:
        print("FAIL: some objects missing or Ontel impacted.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
