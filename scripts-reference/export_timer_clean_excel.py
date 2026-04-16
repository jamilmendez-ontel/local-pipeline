"""
Export clean timer activities from Supabase to a single Excel file.

Pulls from stg_timer_activities_clean (corrected, deduplicated, removals excluded).
All projects in one sheet, sorted by project then start_time.
Same 16 columns as the per-project timer export.

Usage:
    python export_timer_clean_excel.py                  # export + upload + email
    python export_timer_clean_excel.py --no-upload       # local file only
    python export_timer_clean_excel.py --recipients a@ontel.co b@ontel.co

Output: scripts-reference/data_sample/timer_clean_exports/
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
ENV_PATH = SCRIPT_DIR.parent / "swift_api_pipeline" / ".env"
OUTPUT_DIR = SCRIPT_DIR / "data_sample" / "timer_clean_exports"

EXCEL_COLUMNS = [
    "Project",
    "Site Name",
    "Site ID",
    "Task",
    "Site Lat",
    "User Lat",
    "Site Long",
    "User Long",
    "User Accuracy (m)",
    "Site vs User (km)",
    "Start Time",
    "End Time",
    "Duration (min)",
    "User Name",
    "User Email",
    "User Role",
]

NUM_COLS = len(EXCEL_COLUMNS)
DATETIME_COLS = frozenset({
    EXCEL_COLUMNS.index("Start Time"),
    EXCEL_COLUMNS.index("End Time"),
})
REGULAR_COLS = frozenset(set(range(NUM_COLS)) - DATETIME_COLS)

PIPELINE_NAME = "timer_extract"

QUERY = """
SELECT
    t.project,
    t.site_name,
    t.site_id,
    t.task,
    t.site_lat,
    t.user_lat,
    t.site_long,
    t.user_long,
    t.user_accuracy_m,
    t.site_vs_user_km,
    t.start_time,
    t.end_time,
    t.duration_min,
    t.user_name,
    t.user_email,
    t.user_role
FROM data_staging.stg_timer_activities_clean t
WHERE t.start_time >= $1
  AND t.start_time < $2
ORDER BY t.project, t.start_time
"""

DRIVE_FOLDER_NAME = "Timer Clean Data Exports"
EMAIL_RECIPIENTS = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
]


async def check_pipeline_guard(conn):
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

    status = row["status"]
    started = row["started_et"]
    records = row["records_extracted"] or 0
    error = row["error_message"]

    print(f"Pipeline guard: {PIPELINE_NAME}")
    print(f"  Latest run : {started:%Y-%m-%d %H:%M} ET | status={status} | rows={records:,}")

    if status != "success" or error:
        raise SystemExit(
            f"GUARD FAILED: Latest '{PIPELINE_NAME}' run is not successful "
            f"(status={status}, error={error}). Aborting export."
        )
    print("  Guard passed.\n")


def get_month_boundaries(export_dt):
    """Return (month_start_utc, month_end_utc) for the current export month."""
    if export_dt.day == 1:
        prev_month = export_dt.replace(day=1) - timedelta(days=1)
        month_start = prev_month.replace(day=1)
    else:
        month_start = export_dt.replace(day=1)

    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    month_start_utc = datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=ET
    ).astimezone(timezone.utc)
    month_end_utc = datetime(
        month_end.year, month_end.month, month_end.day, tzinfo=ET
    ).astimezone(timezone.utc)

    return month_start, month_end, month_start_utc, month_end_utc


def make_filename(export_dt, month_start):
    yyyymm = month_start.strftime("%Y%m")
    yyyymmdd = export_dt.strftime("%Y%m%d")
    return f"TimerCleanData_{yyyymm}_{yyyymmdd}.xlsx"


def write_workbook(file_path, rows):
    workbook = xlsxwriter.Workbook(str(file_path), {"constant_memory": True})
    header_fmt = workbook.add_format({"bold": True})
    datetime_fmt = workbook.add_format({"num_format": "m/d/yy h:mm"})

    worksheet = workbook.add_worksheet("Clean Data")
    ws_write = worksheet.write
    ws_write_dt = worksheet.write_datetime
    _ET = ET
    _utc = timezone.utc

    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        ws_write(0, col_idx, col_name, header_fmt)

    for row_idx, record in enumerate(rows, start=1):
        for ci in REGULAR_COLS:
            val = record[ci]
            if val is not None:
                ws_write(row_idx, ci, val)

        for ci in DATETIME_COLS:
            val = record[ci]
            if val is not None:
                if val.tzinfo is not None:
                    val = val.astimezone(_ET)
                else:
                    val = val.replace(tzinfo=_utc).astimezone(_ET)
                ws_write_dt(row_idx, ci, val.replace(tzinfo=None), datetime_fmt)

    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        worksheet.set_column(col_idx, col_idx, max(len(col_name) + 2, 12))

    workbook.close()


async def export(output_dir):
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
    await check_pipeline_guard(conn)

    export_dt = datetime.now(ET)
    month_start, month_end, month_start_utc, month_end_utc = get_month_boundaries(export_dt)
    print(f"Export month: {month_start:%Y-%m-%d} to {month_end:%Y-%m-%d} ET")
    print(f"Source: stg_timer_activities_clean\n")

    try:
        t_q = time.time()
        rows = await conn.fetch(QUERY, month_start_utc, month_end_utc)
        t_fetched = time.time()

        count = len(rows)
        file_path = output_dir / make_filename(export_dt, month_start)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, write_workbook, file_path, rows)

        print(
            f"Exported {count:,} rows "
            f"(fetch {t_fetched - t_q:.1f}s, write {time.time() - t_fetched:.1f}s) "
            f"-> {file_path.name}"
        )

        meta = await conn.fetchrow("""
            SELECT started_at AT TIME ZONE 'America/New_York' AS latest_loaded_at,
                   run_id
            FROM pipeline.pipeline_runs
            WHERE pipeline_name = $1 AND status = 'success'
            ORDER BY started_at DESC LIMIT 1
        """, PIPELINE_NAME)
        loaded_at = meta["latest_loaded_at"] if meta else None
        run_id = str(meta["run_id"]) if meta else None

    finally:
        await conn.close()

    elapsed = time.time() - t_start
    print(f"\nTotal: {count:,} rows in {elapsed:.1f}s")
    if loaded_at:
        print(f"Data loaded at: {loaded_at:%Y-%m-%d %I:%M %p} ET")

    return file_path, count, loaded_at, run_id, month_start


def upload_to_drive(file_path):
    from gmail_client import authenticate_drive

    drive_service = authenticate_drive()

    # Find or create folder
    result = drive_service.files().list(
        q=f"name = '{DRIVE_FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()
    files = result.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = drive_service.files().create(
            body={"name": DRIVE_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        folder_id = folder["id"]
        print(f"Created Drive folder: {DRIVE_FOLDER_NAME}")

    # Upload or update
    from googleapiclient.http import MediaFileUpload

    filename = file_path.name
    result = drive_service.files().list(
        q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
        spaces="drive",
        fields="files(id)",
    ).execute()
    existing = result.get("files", [])

    media = MediaFileUpload(str(file_path), resumable=True)

    if existing:
        file_id = existing[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
        print(f"Updated in Drive: {filename}")
    else:
        file_obj = drive_service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
        ).execute()
        file_id = file_obj["id"]
        print(f"Uploaded to Drive: {filename}")

    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def send_email(file_path, row_count, drive_link, loaded_at, run_id, month_start, recipients=None):
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service = authenticate()

    today_et = datetime.now(ET).strftime("%B %d, %Y")
    month_label = month_start.strftime("%B %Y")
    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    loaded_at_str = loaded_at.strftime("%B %d, %Y %I:%M %p ET") if loaded_at else "Unknown"

    subject = f"Timer Clean Data Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Timer Clean Data Export</h2>
    <p>The daily clean timer data export for <strong>{month_label}</strong> is ready.</p>
    <p>This file contains <strong>corrected and deduplicated</strong> timer entries
    from <code>stg_timer_activities_clean</code> — all projects in a single file.</p>

    <table style="border-collapse: collapse; margin: 16px 0; border: 1px solid #ddd;">
        <tr style="background:#f5f5f5;">
            <th style="padding:6px 12px; text-align:left; border-bottom:1px solid #ddd;">File</th>
            <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">Rows</th>
            <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">Size</th>
            <th style="padding:6px 12px; border-bottom:1px solid #ddd;">Link</th>
        </tr>
        <tr>
            <td style="padding:4px 12px;">{file_path.name}</td>
            <td style="padding:4px 12px; text-align:right;">{row_count:,}</td>
            <td style="padding:4px 12px; text-align:right;">{file_size_mb:.1f} MB</td>
            <td style="padding:4px 12px;">
                <a href="{drive_link}" style="color:#1a73e8; font-weight:bold;">Download</a>
            </td>
        </tr>
    </table>

    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding:4px 12px; font-weight:bold;">Data Period</td>
            <td style="padding:4px 12px;">{month_label}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Data Source</td>
            <td style="padding:4px 12px;">stg_timer_activities_clean (corrected + deduplicated)</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Pipeline Run</td>
            <td style="padding:4px 12px;">{loaded_at_str}</td></tr>
    </table>
    </body></html>
    """

    msg = MIMEMultipart()
    msg["To"] = ", ".join(recipients)
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent to {', '.join(recipients)}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Export clean timer data to a single Excel file")
    parser.add_argument("--no-upload", action="store_true", help="Skip Drive upload and email")
    parser.add_argument("--recipients", nargs="+", help="Override email recipients")
    args = parser.parse_args()

    file_path, row_count, loaded_at, run_id, month_start = asyncio.run(export(OUTPUT_DIR))

    if not args.no_upload:
        try:
            print("\nUploading to Google Drive...")
            t0 = time.time()
            drive_link = upload_to_drive(file_path)
            print(f"Upload took {time.time() - t0:.1f}s")

            print("Sending email notification...")
            send_email(file_path, row_count, drive_link, loaded_at, run_id,
                       month_start, recipients=args.recipients)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("Excel file was still generated successfully.")


if __name__ == "__main__":
    main()
