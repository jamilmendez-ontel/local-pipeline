"""
Export user priorities from stg_user_priorities to Excel.
Matches the "My Tasks" file format used in the shared Drive.

Usage:
    python export_user_priorities_excel.py
    python export_user_priorities_excel.py --no-upload

Output goes to scripts-reference/data_sample/user_priorities_exports/
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import xlsxwriter
from dotenv import load_dotenv

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "swift_api_pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH   = SCRIPT_DIR.parent / "swift_api_pipeline" / ".env"
OUTPUT_DIR = SCRIPT_DIR / "data_sample" / "user_priorities_exports"

PIPELINE_NAME = "user_priorities_extract"

# Column order matches the existing "My Tasks" file in the shared Drive
COLUMNS = [
    ("organization",     "Organization"),
    ("project",          "Project"),
    ("asset_name",       "Asset Name"),
    ("asset_id",         "Asset Id"),
    ("milestone",        "Milestone"),
    ("task_name",        "Task Name"),
    ("pin_type",         "Pin Type"),
    ("display_date",     "Display Date"),
    ("status",           "Status"),
    ("calendar_status",  "Calendar Status"),
    ("assigned_to",      "Assigned To"),
    ("scheduled",        "Scheduled"),
    ("scheduled_by",     "Scheduled By"),
    ("submitted_by",     "Submitted By"),
    ("submitted_on",     "Submitted On"),
    ("approved_by",      "Approved By"),
    ("approved_on",      "Approved On"),
    ("cancelled_by",     "Cancelled By"),
    ("cancelled_on",     "Cancelled On"),
    ("duration",         "Duration"),
    ("rejected_on",      "Rejected On"),
    ("rejected_by",      "Rejected By"),
]

DB_COLS   = [c[0] for c in COLUMNS]
HDR_NAMES = [c[1] for c in COLUMNS]

DATETIME_COLS = {"display_date", "scheduled", "submitted_on", "approved_on",
                 "cancelled_on", "rejected_on"}
DATETIME_IDX  = frozenset(i for i, (col, _) in enumerate(COLUMNS) if col in DATETIME_COLS)
REGULAR_IDX   = frozenset(range(len(COLUMNS))) - DATETIME_IDX

QUERY = f"""
SELECT {', '.join(DB_COLS)}
FROM data_staging.stg_user_priorities
WHERE run_id = $1
ORDER BY organization, project, asset_name, task_name
"""


async def check_pipeline_guard(conn):
    """Abort if the latest user_priorities_extract run failed."""
    row = await conn.fetchrow("""
        SELECT status, records_extracted, error_message,
               started_at AT TIME ZONE 'America/New_York' AS started_et
        FROM pipeline.pipeline_runs
        WHERE pipeline_name = $1
        ORDER BY started_at DESC
        LIMIT 1
    """, PIPELINE_NAME)

    if row is None:
        raise SystemExit(f"GUARD FAILED: No pipeline runs found for '{PIPELINE_NAME}'.")

    status  = row["status"]
    started = row["started_et"]
    records = row["records_extracted"] or 0
    error   = row["error_message"]

    print(f"Pipeline guard: {PIPELINE_NAME}")
    print(f"  Latest run : {started:%Y-%m-%d %H:%M} ET | status={status} | rows={records:,}")

    if status != "success" or error:
        raise SystemExit(
            f"GUARD FAILED: Latest '{PIPELINE_NAME}' run is not successful "
            f"(status={status}, error={error})."
        )
    print("  Guard passed.\n")


def write_workbook(file_path: Path, rows: list):
    """Write a single-sheet workbook with all user priorities."""
    workbook = xlsxwriter.Workbook(str(file_path), {"constant_memory": True})
    header_fmt   = workbook.add_format({"bold": True})
    datetime_fmt = workbook.add_format({"num_format": "m/d/yy h:mm"})

    worksheet = workbook.add_worksheet("Sheet1")
    ws_write    = worksheet.write
    ws_write_dt = worksheet.write_datetime
    _ET  = ET
    _utc = timezone.utc

    for col_idx, hdr in enumerate(HDR_NAMES):
        ws_write(0, col_idx, hdr, header_fmt)

    for row_idx, record in enumerate(rows, start=1):
        for ci in REGULAR_IDX:
            val = record[ci]
            if val is not None:
                ws_write(row_idx, ci, val)

        for ci in DATETIME_IDX:
            val = record[ci]
            if val is not None:
                if val.tzinfo is not None:
                    val = val.astimezone(_ET)
                else:
                    val = val.replace(tzinfo=_utc).astimezone(_ET)
                ws_write_dt(row_idx, ci, val.replace(tzinfo=None), datetime_fmt)

    for col_idx, hdr in enumerate(HDR_NAMES):
        worksheet.set_column(col_idx, col_idx, max(len(hdr) + 2, 12))

    workbook.close()


async def export(output_dir: Path):
    """Export user priorities to a single Excel file."""
    load_dotenv(ENV_PATH)
    t_start = time.time()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
    )
    await conn.execute("SET statement_timeout = '300s'")

    try:
        await check_pipeline_guard(conn)

        run_row = await conn.fetchrow("""
            SELECT run_id FROM pipeline.pipeline_runs
            WHERE pipeline_name = $1
            ORDER BY started_at DESC LIMIT 1
        """, PIPELINE_NAME)
        run_id = run_row["run_id"]

        rows = await conn.fetch(QUERY, run_id)

        meta = await conn.fetchrow("""
            SELECT started_at AT TIME ZONE 'America/New_York' AS started_et
            FROM pipeline.pipeline_runs
            WHERE pipeline_name = $1 AND status = 'success'
            ORDER BY started_at DESC LIMIT 1
        """, PIPELINE_NAME)
        loaded_at = meta["started_et"] if meta else None
    finally:
        await conn.close()

    export_dt = datetime.now(ET)
    filename  = f"My Tasks {export_dt.strftime('%m-%d-%Y')}.xlsx"
    file_path = output_dir / filename

    write_workbook(file_path, rows)

    elapsed = time.time() - t_start
    size_mb = file_path.stat().st_size / (1024 * 1024)
    print(f"Exported: {filename} ({len(rows):,} rows, {size_mb:.1f} MB, {elapsed:.1f}s)")
    if loaded_at:
        print(f"Data loaded at: {loaded_at:%Y-%m-%d %I:%M %p} ET")

    return file_path, len(rows), loaded_at


EMAIL_RECIPIENTS = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
]


def send_export_email(file_path: Path, row_count: int, loaded_at=None, recipients=None):
    """Send email notification with file summary."""
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service  = authenticate()
    today_et = datetime.now(ET).strftime("%B %d, %Y")
    loaded_at_str = loaded_at.strftime("%B %d, %Y %I:%M %p ET") if loaded_at else "Unknown"
    size_mb  = file_path.stat().st_size / (1024 * 1024)

    subject = f"User Priorities Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>User Priorities Export</h2>
    <p>The user priorities (My Tasks) export is ready.</p>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding:4px 12px; font-weight:bold;">File</td>
            <td style="padding:4px 12px;">{file_path.name}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Rows</td>
            <td style="padding:4px 12px;">{row_count:,}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Size</td>
            <td style="padding:4px 12px;">{size_mb:.1f} MB</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Data Loaded At</td>
            <td style="padding:4px 12px;">{loaded_at_str}</td></tr>
    </table>
    </body></html>
    """

    msg = MIMEMultipart()
    msg["To"]      = ", ".join(recipients)
    msg["From"]    = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent to {', '.join(recipients)}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Export user priorities to Excel")
    parser.add_argument("--no-upload", action="store_true", help="Skip email notification")
    parser.add_argument("--recipients", nargs="+", help="Override email recipients")
    args = parser.parse_args()

    file_path, row_count, loaded_at = asyncio.run(export(OUTPUT_DIR))

    if not args.no_upload:
        try:
            print("Sending email notification...")
            send_export_email(file_path, row_count, loaded_at, recipients=args.recipients)
        except Exception as e:
            print(f"ERROR: Email send failed: {e}")


if __name__ == "__main__":
    main()
