"""Watchdog poller for the in-flight TS13 smoke test.

Emits one line per poll cycle. Prefixes alerts so the Monitor's grep can route them.
Exits cleanly when pipeline_runs shows a fresh asset_tasks_extract completion.
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

POLL_SECONDS = 60
STALL_THRESHOLD_SECONDS = 240  # 4 min without new rows = STALLED alert
MAX_RUNTIME_SECONDS = 1800     # exit after 30 min regardless


async def main():
    import asyncpg
    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl="require",
    )

    start = time.monotonic()
    last_seen_count = -1
    last_growth_ts = time.monotonic()
    print("WATCHDOG_START: polling every 60s, stall=4min, hard-exit=30min", flush=True)

    while True:
        elapsed = int(time.monotonic() - start)

        # 1) TS13 partition growth
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n, "
            "EXTRACT(EPOCH FROM (NOW() - MAX(loaded_at)))::int AS sec_since_last_write "
            "FROM data_raw.raw_asset_tasks_ts13"
        )
        ts13_count = row["n"]
        sec_since = row["sec_since_last_write"] or -1
        if ts13_count != last_seen_count:
            delta = ts13_count - last_seen_count if last_seen_count >= 0 else 0
            print(
                f"PROGRESS t={elapsed}s ts13_rows={ts13_count:,} "
                f"delta={delta:+,} last_write={sec_since}s ago",
                flush=True,
            )
            last_seen_count = ts13_count
            last_growth_ts = time.monotonic()
        else:
            stall_sec = int(time.monotonic() - last_growth_ts)
            if stall_sec >= STALL_THRESHOLD_SECONDS:
                print(
                    f"ALERT STALLED no_new_rows_for={stall_sec}s "
                    f"ts13_rows={ts13_count:,}",
                    flush=True,
                )

        # 2) Pipeline run status check
        pr = await conn.fetchrow(
            "SELECT status, "
            "EXTRACT(EPOCH FROM (NOW() - completed_at))::int AS sec_since_complete, "
            "error_message "
            "FROM pipeline.pipeline_runs "
            "WHERE pipeline_name='asset_tasks_extract' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        if pr:
            status = pr["status"]
            if status == "failed":
                print(
                    f"ALERT FAILED pipeline_runs.status=failed "
                    f"err={pr['error_message'] or ''}",
                    flush=True,
                )
                break
            if status == "success" and pr["sec_since_complete"] is not None and pr["sec_since_complete"] < 300:
                # Recent success — smoke must have finished extract phase
                print(
                    f"DONE_EXTRACT status=success sec_since_complete={pr['sec_since_complete']}s "
                    f"ts13_rows={ts13_count:,}",
                    flush=True,
                )
                break

        # 3) Active sessions still doing work?
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM pg_stat_activity "
            "WHERE state='active' AND application_name NOT IN ('mgmt-api','psql') "
            "AND query ILIKE '%raw_asset_tasks%'"
        )
        if active > 0:
            pass  # busy, expected
        # No emit — we trust DB growth as the signal

        if elapsed >= MAX_RUNTIME_SECONDS:
            print(f"WATCHDOG_TIMEOUT elapsed={elapsed}s ts13_rows={ts13_count:,}", flush=True)
            break

        await asyncio.sleep(POLL_SECONDS)

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
