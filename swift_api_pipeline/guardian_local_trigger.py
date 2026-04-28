"""Runs on local PC via Task Scheduler every 5 min.

Reads agent.monitor_state for rows with state='approved' and
proposed_action.function = 'trigger_local_pipeline', launches the pipeline,
and marks the row 'executed'.

This is the bridge between cloud Guardian decisions and local pipeline execution.
"""
import asyncio
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

MAIN_PY = SCRIPT_DIR / "main.py"
VENV_PYTHON = SCRIPT_DIR / "venv" / "Scripts" / "python.exe"


async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB", "postgres"),
        user=os.getenv("SUPABASE_USER", "postgres"),
        password=os.environ["SUPABASE_PASSWORD"],
        ssl=ssl_ctx,
        statement_cache_size=0,
    )
    try:
        rows = await conn.fetch("""
            SELECT id, pipeline_name, proposed_action
            FROM agent.monitor_state
            WHERE state = 'approved'
              AND proposed_action->>'function' = 'trigger_local_pipeline'
            ORDER BY created_at ASC
            LIMIT 5
        """)

        if not rows:
            return

        for r in rows:
            pipeline_name = r["pipeline_name"]
            proposed = r["proposed_action"]
            if isinstance(proposed, str):
                proposed = json.loads(proposed)

            # Map pipeline_name to --pipeline CLI flag
            cli_map = {
                "asset_tasks_extract": "asset_tasks",
                "calendar_leave": "calendar",
            }
            cli_flag = cli_map.get(pipeline_name)
            if not cli_flag:
                print(f"Skipping unknown local pipeline: {pipeline_name}")
                await conn.execute(
                    """UPDATE agent.monitor_state
                       SET state = 'escalated',
                           executed_at = NOW(),
                           result = $2::jsonb
                       WHERE id = $1""",
                    r["id"],
                    json.dumps({"error": f"unknown local pipeline: {pipeline_name}"}),
                )
                continue

            print(f"Launching: {VENV_PYTHON} main.py --pipeline {cli_flag}")
            proc = subprocess.Popen(
                [str(VENV_PYTHON), str(MAIN_PY), "--pipeline", cli_flag],
                cwd=str(SCRIPT_DIR),
            )

            # Fire-and-forget — the pipeline's own notifier sends success/failure email
            await conn.execute(
                """UPDATE agent.monitor_state
                   SET state = 'executed',
                       executed_at = NOW(),
                       result = $2::jsonb
                   WHERE id = $1""",
                r["id"],
                json.dumps({"launched_pid": proc.pid, "cli_flag": cli_flag, "via": "local_trigger"}),
            )
            print(f"  -> PID {proc.pid}, marked as executed")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
