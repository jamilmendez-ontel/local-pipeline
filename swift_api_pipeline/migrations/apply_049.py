"""Apply migration 049: Overlap-based duplicate detection support."""
import asyncio
import ssl
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

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl=ctx,
    )

    sql_path = Path(__file__).with_name("049_timer_overlap_dup_detection.sql")
    sql = sql_path.read_text(encoding="utf-8")

    # Pre-flight snapshot
    before_with_start = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "AND entries->0 ? 'start_time'"
    )
    before_total = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0"
    )
    print(f"Before: {before_with_start}/{before_total} review rows already have entries[].start_time")

    print("Applying migration 049: Overlap-based duplicate detection support...")
    await conn.execute(sql)
    print("Migration 049 applied successfully.")

    # Post-verify backfill
    after_with_start = await conn.fetchval(
        "SELECT COUNT(*) FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "AND entries->0 ? 'start_time'"
    )
    print(f"After:  {after_with_start}/{before_total} review rows have entries[].start_time")
    assert after_with_start == before_total, (
        f"Backfill incomplete: {after_with_start}/{before_total} rows have entries[].start_time"
    )

    # Spot-check: a random row's entries[0].start_time matches the parent start_time
    sample = await conn.fetchrow(
        "SELECT group_id, start_time, entries->0->>'start_time' AS first_entry_start "
        "FROM data_staging.stg_timer_duplicate_reviews "
        "WHERE entries IS NOT NULL AND jsonb_array_length(entries) > 0 "
        "LIMIT 1"
    )
    if sample:
        print(f"Spot-check {sample['group_id']}: parent={sample['start_time']}, "
              f"entries[0]={sample['first_entry_start']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
