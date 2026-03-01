"""
Export timer activities from Supabase to Excel — one workbook per TECH-OPS project (TS13-TS18).
Matches the format of the timer_data_sample files (TimeData_TS{num}_{YYYYMM}_{YYYYMMDD}.xlsx).

Each workbook has a single sheet named "Sheet1" with 16 columns.

Usage:
    python export_timer_excel.py
    python export_timer_excel.py --no-upload

Output goes to scripts-reference/data_sample/timer_exports/ by default.
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import xlsxwriter
from dotenv import load_dotenv

# Add swift_api_pipeline to path for gmail_client imports
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "swift_api_pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR.parent / "swift_api_pipeline" / ".env"
OUTPUT_DIR = SCRIPT_DIR / "data_sample" / "timer_exports"

PROJECTS = [
    "TECH-OPS: TS13",
    "TECH-OPS: TS14",
    "TECH-OPS: TS15",
    "TECH-OPS: TS16",
    "TECH-OPS: TS17",
    "TECH-OPS: TS18",
]

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


async def check_pipeline_guard(conn):
    """Abort if the latest timer_extract run failed."""
    row = await conn.fetchrow("""
        SELECT status, records_extracted, error_message,
               started_at AT TIME ZONE 'America/New_York' AS started_et
        FROM pipeline.pipeline_runs
        WHERE pipeline_name = $1
        ORDER BY started_at DESC
        LIMIT 1
    """, PIPELINE_NAME)

    if row is None:
        raise SystemExit(f"GUARD FAILED: No pipeline runs found for '{PIPELINE_NAME}'. Aborting export.")

    status  = row["status"]
    started = row["started_et"]
    records = row["records_extracted"] or 0
    error   = row["error_message"]

    print(f"Pipeline guard: {PIPELINE_NAME}")
    print(f"  Latest run : {started:%Y-%m-%d %H:%M} ET | status={status} | rows={records:,}")

    if status != "success" or error:
        raise SystemExit(
            f"GUARD FAILED: Latest '{PIPELINE_NAME}' run is not successful "
            f"(status={status}, error={error}). Aborting export."
        )

    print("  Guard passed.\n")


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
FROM data_staging.stg_timer_activities t
WHERE t.project = $1 AND t.run_id = $2
ORDER BY t.start_time, t.site_name
"""


def make_filename(ts_label: str, export_dt: datetime) -> str:
    """Generate filename matching sample convention: TimeData_TS{num}_{YYYYMM}_{YYYYMMDD}.xlsx
    On the 1st of the month, YYYYMM reflects the previous month since the data
    inside is for the prior month (e.g. 202602_20260301 for Feb data exported Mar 1).
    """
    if export_dt.day == 1:
        prev_month = export_dt.replace(day=1) - timedelta(days=1)
        yyyymm = prev_month.strftime("%Y%m")
    else:
        yyyymm = export_dt.strftime("%Y%m")
    yyyymmdd = export_dt.strftime("%Y%m%d")
    return f"TimeData_{ts_label}_{yyyymm}_{yyyymmdd}.xlsx"


def write_workbook(file_path: Path, rows: list):
    """Write a single-sheet workbook for one project."""
    workbook = xlsxwriter.Workbook(str(file_path), {"constant_memory": True})
    header_fmt = workbook.add_format({"bold": True})
    datetime_fmt = workbook.add_format({"num_format": "m/d/yy h:mm"})

    worksheet = workbook.add_worksheet("Sheet1")
    ws_write = worksheet.write
    ws_write_dt = worksheet.write_datetime
    _ET = ET
    _utc = timezone.utc

    # Header row
    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        ws_write(0, col_idx, col_name, header_fmt)

    # Data rows
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

    # Column widths
    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        worksheet.set_column(col_idx, col_idx, max(len(col_name) + 2, 12))

    workbook.close()


async def export(output_dir: Path):
    """Export one workbook per project. Returns list of (ts_label, file_path, row_count)."""
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

    # Get run_id from latest successful pipeline run
    run_row = await conn.fetchrow("""
        SELECT run_id FROM pipeline.pipeline_runs
        WHERE pipeline_name = $1
        ORDER BY started_at DESC LIMIT 1
    """, PIPELINE_NAME)
    run_id_uuid = run_row["run_id"]

    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_event_loop()
    export_dt = datetime.now(ET)
    results = []

    try:
        # Pipeline: prefetch next project while writing current one
        next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[0], run_id_uuid))

        for i, project_name in enumerate(PROJECTS):
            ts_label = project_name.split(": ")[1]
            t_q = time.time()

            rows = await next_fetch
            t_fetched = time.time()

            if i + 1 < len(PROJECTS):
                next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[i + 1], run_id_uuid))

            count = len(rows)
            file_path = output_dir / make_filename(ts_label, export_dt)

            await loop.run_in_executor(executor, write_workbook, file_path, rows)
            t_written = time.time()

            print(
                f"{ts_label}: {count:>9,} rows  "
                f"(fetch {t_fetched - t_q:.1f}s, write {t_written - t_fetched:.1f}s)"
                f"  -> {file_path.name}"
            )
            results.append((ts_label, file_path, count))

        # Fetch latest loaded_at for email metadata
        meta = await conn.fetchrow("""
            SELECT MAX(loaded_at) AT TIME ZONE 'America/New_York' AS latest_loaded_at
            FROM data_staging.stg_timer_activities
            WHERE run_id = $1
        """, run_id_uuid)
        loaded_at = meta["latest_loaded_at"] if meta else None
        run_id = str(run_id_uuid)

    finally:
        await conn.close()
        executor.shutdown(wait=False)

    total_rows = sum(r[2] for r in results)
    elapsed = time.time() - t_start
    print(f"\nTotal: {total_rows:,} rows across {len(results)} files in {elapsed:.1f}s")
    if loaded_at:
        print(f"Data loaded at: {loaded_at:%Y-%m-%d %I:%M %p} ET")
        print(f"Run ID: {run_id}")

    return results, loaded_at, run_id


# Google Drive folder name for exports
DRIVE_FOLDER_NAME = "Timer Data Exports"
EMAIL_RECIPIENTS = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
]


def get_or_create_drive_folder(drive_service, folder_name: str) -> str:
    """Find existing folder by name, or create it. Returns folder ID."""
    result = drive_service.files().list(
        q=f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive_service.files().create(body=metadata, fields="id").execute()
    print(f"Created Drive folder: {folder_name}")
    return folder["id"]


def upload_file_to_drive(drive_service, folder_id: str, file_path: Path) -> str:
    """Upload a single file to an existing Drive folder. Returns shareable link."""
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
        print(f"  Updated in Drive: {filename}")
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file_obj = drive_service.files().create(
            body=metadata, media_body=media, fields="id",
        ).execute()
        file_id = file_obj["id"]
        print(f"  Uploaded to Drive: {filename}")

    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def upload_all_to_drive(results: list) -> list:
    """Upload all project files to Drive. Returns list of (ts_label, file_path, row_count, drive_link)."""
    from gmail_client import authenticate_drive

    drive_service = authenticate_drive()
    folder_id = get_or_create_drive_folder(drive_service, DRIVE_FOLDER_NAME)

    enriched = []
    for ts_label, file_path, row_count in results:
        link = upload_file_to_drive(drive_service, folder_id, file_path)
        enriched.append((ts_label, file_path, row_count, link))

    return enriched


def send_export_email(enriched_results: list, loaded_at=None, run_id=None, recipients=None):
    """Send one email with a table of all project files and their Drive links."""
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service = authenticate()

    today_et = datetime.now(ET).strftime("%B %d, %Y")
    total_rows = sum(r[2] for r in enriched_results)
    loaded_at_str = loaded_at.strftime("%B %d, %Y %I:%M %p ET") if loaded_at else "Unknown"
    run_id_str = run_id or "Unknown"

    # Build one row per project
    rows_html = ""
    for ts_label, file_path, row_count, drive_link in enriched_results:
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        rows_html += (
            f"<tr>"
            f"<td style='padding:4px 12px;'>{ts_label}</td>"
            f"<td style='padding:4px 12px;'>{file_path.name}</td>"
            f"<td style='padding:4px 12px; text-align:right;'>{row_count:,}</td>"
            f"<td style='padding:4px 12px; text-align:right;'>{file_size_mb:.1f} MB</td>"
            f"<td style='padding:4px 12px;'>"
            f"<a href='{drive_link}' style='color:#1a73e8;'>Download</a>"
            f"</td>"
            f"</tr>"
        )

    subject = f"Timer Data Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Timer Data Export</h2>
    <p>The daily timer data export is ready — one file per project.</p>
    <table style="border-collapse: collapse; margin: 16px 0; border: 1px solid #ddd;">
        <thead>
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 12px; text-align:left; border-bottom:1px solid #ddd;">Project</th>
                <th style="padding:6px 12px; text-align:left; border-bottom:1px solid #ddd;">File</th>
                <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">Rows</th>
                <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">Size</th>
                <th style="padding:6px 12px; border-bottom:1px solid #ddd;">Link</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding:4px 12px; font-weight:bold;">Total Rows</td>
            <td style="padding:4px 12px;">{total_rows:,}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Data Loaded At</td>
            <td style="padding:4px 12px;">{loaded_at_str}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Pipeline Run ID</td>
            <td style="padding:4px 12px;"><code>{run_id_str}</code></td></tr>
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
    parser = argparse.ArgumentParser(description="Export timer activities to Excel (one file per project)")
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Drive upload and email",
    )
    args = parser.parse_args()

    results, loaded_at, run_id = asyncio.run(export(OUTPUT_DIR))

    if not args.no_upload:
        try:
            print("\nUploading to Google Drive...")
            t0 = time.time()
            enriched = upload_all_to_drive(results)
            print(f"Upload took {time.time() - t0:.1f}s")

            print("Sending email notification...")
            send_export_email(enriched, loaded_at, run_id)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("Excel files were still generated successfully.")


if __name__ == "__main__":
    main()
