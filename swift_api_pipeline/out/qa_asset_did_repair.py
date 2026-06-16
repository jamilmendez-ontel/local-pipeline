"""
One-time QA-form asset_did re-derivation for migration 104 (QA portion).
Re-derives EVERY stg_qa_form row with the new backfill_asset_did() logic
(Pass A path+name, Pass B name-unique; old "take-first" + lookup dropped).

Steps:
  1. NULL all stg_qa_form.asset_did + run backfill_asset_did()  [one txn]
  2. print before/after verification counts

Note: timer dids are already correct; backfill's timer passes only re-touch the
remaining timer NULLs (no change). No clean-table rebuild needed for QA (asset_did
lives directly on stg_qa_form; analytics.v_qa_forms is a live view).

Run: venv\\Scripts\\python.exe out\\qa_asset_did_repair.py
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
            "SELECT COUNT(*) total, COUNT(asset_did) linked FROM data_staging.stg_qa_form"
        )
        print(f"BEFORE: total={before['total']} linked={before['linked']}")

        async with conn.transaction():
            await conn.execute(
                "UPDATE data_staging.stg_qa_form SET asset_did = NULL WHERE asset_did IS NOT NULL"
            )
            row = await conn.fetchrow("SELECT * FROM data_staging.backfill_asset_did()")
            print(f"backfill_asset_did -> timer_updated={row['timer_updated']} qa_form_updated={row['qa_form_updated']}")

        after = await conn.fetchrow(
            "SELECT COUNT(*) total, COUNT(asset_did) linked FROM data_staging.stg_qa_form"
        )
        print(f"AFTER : total={after['total']} linked={after['linked']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
