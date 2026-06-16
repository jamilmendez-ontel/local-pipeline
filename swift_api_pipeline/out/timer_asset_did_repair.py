"""
One-time timer asset_did re-derivation for migration 104.
Re-derives EVERY timer row (old + new) with the new backfill_asset_did() logic,
then rebuilds the user-facing clean table.

Steps (timer only; QA dids are left untouched here):
  1. NULL all stg_timer_activities.asset_did  + run backfill_asset_did()  [one txn]
  2. rebuild_timer_clean()
  3. print before/after verification counts

Run: venv\\Scripts\\python.exe out\\timer_asset_did_repair.py
"""
import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PROJECT_REF = "voqfjfngdpcvevbkikud"
DSN = dict(
    host="aws-0-ap-southeast-1.pooler.supabase.com",
    port=6543,
    database=os.environ.get("SUPABASE_DB", "postgres"),
    user=f"postgres.{PROJECT_REF}",
    password=os.environ["SUPABASE_PASSWORD"],
    statement_cache_size=0,
    ssl="require",
    command_timeout=900,
)


async def main():
    conn = await asyncpg.connect(**DSN)
    try:
        before = await conn.fetchrow(
            "SELECT COUNT(*) total, COUNT(asset_did) linked FROM data_staging.stg_timer_activities"
        )
        print(f"BEFORE: total={before['total']} linked={before['linked']}")

        # Step 1: NULL all + re-derive, atomically
        async with conn.transaction():
            await conn.execute(
                "UPDATE data_staging.stg_timer_activities SET asset_did = NULL WHERE asset_did IS NOT NULL"
            )
            row = await conn.fetchrow("SELECT * FROM data_staging.backfill_asset_did()")
            print(f"backfill_asset_did -> timer_updated={row['timer_updated']} qa_form_updated={row['qa_form_updated']}")

        # Step 2: rebuild user-facing clean table
        await conn.execute("SELECT data_staging.rebuild_timer_clean()")
        print("rebuild_timer_clean -> done")

        # Step 3: verify
        after = await conn.fetchrow(
            "SELECT COUNT(*) total, COUNT(asset_did) linked FROM data_staging.stg_timer_activities"
        )
        clean = await conn.fetchrow(
            "SELECT COUNT(*) total, COUNT(asset_did) linked FROM data_staging.stg_timer_activities_clean"
        )
        print(f"AFTER  base : total={after['total']} linked={after['linked']}")
        print(f"AFTER  clean: total={clean['total']} linked={clean['linked']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
