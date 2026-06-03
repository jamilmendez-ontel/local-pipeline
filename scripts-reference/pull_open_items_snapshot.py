"""Dump the current open-items-report scope from stg_user_priorities to Excel
so we can diff against the user's manual punch_item_extractor tool output.

Usage (from local-pipeline/swift_api_pipeline so .env is found):
    cd local-pipeline/swift_api_pipeline
    ./venv/Scripts/python.exe ../scripts-reference/pull_open_items_snapshot.py
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import asyncio
import asyncpg
from openpyxl import Workbook
from dotenv import load_dotenv

# Load .env from swift_api_pipeline (current working dir at invocation)
load_dotenv()

SQL = """
SELECT
  up.organization AS "Organization",
  up.project AS "Project",
  up.asset_id AS "Asset Id",
  up.asset_name AS "Asset Name",
  up.assigned_to AS "Assigned To",
  up.status AS "Status",
  up.task_name AS "Task Name",
  up.task_did AS "Task DID",
  'https://swiftprojects.io/#/app/assets/tasks/' || up.task_did AS "URL",
  (up.loaded_at AT TIME ZONE 'America/New_York')::text AS "Loaded ET"
FROM data_staging.stg_user_priorities up
JOIN reference.report_targets rt
  ON rt.report_name = 'open_items_report'
 AND rt.enabled
 AND rt.org_did = up.org_did
 AND rt.project_did = up.project_did
WHERE up.task_name ILIKE '%punch%'
  AND up.status IN ('pending','in_progress')
  AND up.assigned_to IS NOT NULL
ORDER BY up.project, up.asset_name, up.task_name;
"""

async def main():
    dsn = (
        f"postgres://{os.environ['SUPABASE_USER']}:{os.environ['SUPABASE_PASSWORD']}"
        f"@{os.environ['SUPABASE_HOST']}:{os.environ['SUPABASE_PORT']}/{os.environ['SUPABASE_DB']}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(SQL)
    finally:
        await conn.close()

    if not rows:
        print("No rows returned.")
        return

    headers = list(rows[0].keys())
    print(f"Pulled {len(rows)} rows.")

    wb = Workbook()
    ws = wb.active
    ws.title = "open_items_supabase"
    ws.append(headers)
    for r in rows:
        ws.append([r[h] for h in headers])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path.home() / "Desktop" / f"open_items_supabase_snapshot_{ts}.xlsx"
    wb.save(out_path)
    print(f"Wrote: {out_path}")

    by_proj = {}
    for r in rows:
        by_proj[r["Project"]] = by_proj.get(r["Project"], 0) + 1
    print("\nPer-project counts:")
    for proj, n in sorted(by_proj.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {proj}")

if __name__ == "__main__":
    asyncio.run(main())
