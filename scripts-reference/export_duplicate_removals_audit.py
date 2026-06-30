"""
One-off audit export: declared-duplicate entries that landed in
app_timer.entry_removals (i.e. duplicates the auto-resolver kept, but were
later removed for other reasons). Writes a single Excel file to the user's
Desktop.

Run:
    python export_duplicate_removals_audit.py
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import xlsxwriter
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR.parent / "swift_api_pipeline" / ".env"
OUTPUT_DIR = Path.home() / "Desktop"

QUERY = """
WITH declared AS (
    SELECT r.id            AS review_id,
           r.group_id, r.status, r.resolved_by, r.resolved_at,
           r.project, r.project_did, r.user_email,
           r.start_time, r.site_name, r.site_id, r.task,
           (e->>'end_time')::timestamptz AS end_time,
           (e->>'duration_min')::numeric AS duration_min,
           r.rejected_entries
    FROM app_timer.duplicate_reviews r
    CROSS JOIN LATERAL jsonb_array_elements(r.entries) AS e
)
SELECT
    d.review_id,
    d.group_id,
    d.status,
    d.resolved_by,
    d.resolved_at AT TIME ZONE 'America/New_York'    AS resolved_at_et,
    d.project,
    d.project_did,
    d.site_name,
    d.site_id,
    d.task,
    d.user_email,
    d.start_time AT TIME ZONE 'America/New_York'     AS start_et,
    d.end_time   AT TIME ZONE 'America/New_York'     AS end_et,
    ROUND(d.duration_min::numeric, 2)                AS duration_min,
    rm.id                                            AS removal_id,
    rm.entry_id                                      AS removal_entry_id,
    rm.reason                                        AS removal_reason,
    rm.removed_at   AT TIME ZONE 'America/New_York'  AS removed_at_et,
    rm.created_at   AT TIME ZONE 'America/New_York'  AS removal_created_at_et
FROM declared d
JOIN app_timer.entry_removals rm
  ON rm.project_did = d.project_did
 AND rm.user_email  = d.user_email
 AND rm.start_time  = d.start_time
 AND rm.site_name IS NOT DISTINCT FROM d.site_name
 AND rm.site_id   IS NOT DISTINCT FROM d.site_id
 AND rm.task      IS NOT DISTINCT FROM d.task
 AND rm.end_time IS NOT DISTINCT FROM d.end_time
 AND rm.duration_min IS NOT DISTINCT FROM d.duration_min
 AND rm.reason IS DISTINCT FROM 'REVERTED'
WHERE NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(COALESCE(d.rejected_entries, '[]'::jsonb)) re
    WHERE (re->>'end_time')::timestamptz = d.end_time
      AND (re->>'duration_min')::numeric = d.duration_min
)
ORDER BY rm.removed_at DESC, d.start_time DESC
"""

COLUMNS = [
    ("review_id",             10),
    ("group_id",              14),
    ("status",                14),
    ("resolved_by",           14),
    ("resolved_at_et",        19),
    ("project",               24),
    ("project_did",           22),
    ("site_name",             40),
    ("site_id",               60),
    ("task",                  36),
    ("user_email",            28),
    ("start_et",              19),
    ("end_et",                19),
    ("duration_min",          12),
    ("removal_id",            10),
    ("removal_entry_id",      16),
    ("removal_reason",        16),
    ("removed_at_et",         19),
    ("removal_created_at_et", 19),
]

DATETIME_KEYS = {"resolved_at_et", "start_et", "end_et", "removed_at_et", "removal_created_at_et"}


async def main():
    load_dotenv(ENV_PATH)

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
    )
    try:
        rows = await conn.fetch(QUERY)
    finally:
        await conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"duplicate_removals_audit_{stamp}.xlsx"

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Removed declared duplicates")

    header_fmt = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
    dt_fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    num_fmt = wb.add_format({"num_format": "0.00"})

    for col_idx, (name, width) in enumerate(COLUMNS):
        ws.set_column(col_idx, col_idx, width)
        ws.write(0, col_idx, name, header_fmt)

    for row_idx, row in enumerate(rows, start=1):
        for col_idx, (name, _) in enumerate(COLUMNS):
            value = row[name]
            if value is None:
                ws.write_blank(row_idx, col_idx, None)
            elif name in DATETIME_KEYS:
                # asyncpg returns datetime objects (naive after AT TIME ZONE)
                ws.write_datetime(row_idx, col_idx, value, dt_fmt)
            elif name == "duration_min":
                ws.write_number(row_idx, col_idx, float(value), num_fmt)
            else:
                ws.write(row_idx, col_idx, value)

    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, len(rows), len(COLUMNS) - 1)
    wb.close()

    print(f"Wrote {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
