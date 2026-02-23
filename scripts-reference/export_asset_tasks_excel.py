"""
Export asset tasks from Supabase to Excel with one tab per TECH-OPS project (TS13-TS18).
Matches the format of the original manually-created 20260109.xlsx file.

Usage:
    python export_asset_tasks_excel.py
    python export_asset_tasks_excel.py --output custom_name.xlsx

Output goes to scripts-reference/data_sample/YYYYMMDD.xlsx by default.
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
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
OUTPUT_DIR = SCRIPT_DIR / "data_sample"

PROJECTS = [
    "TECH-OPS: TS18",
    "TECH-OPS: TS17",
    "TECH-OPS: TS16",
    "TECH-OPS: TS15",
    "TECH-OPS: TS14",
    "TECH-OPS: TS13",
]

EXCEL_COLUMNS = [
    "Source.Name",
    "Project_DID",
    "Project_Status",
    "Asset_DID",
    "Asset_ID",
    "Asset_Name",
    "Asset_Requirement_Count",
    "Task_DID",
    "Task_Name",
    "Task_Status",
    "Task_Scheduled",
    "Task_Assigned_To_DID",
    "Task_Assigned_To_Collection",
    "Task_Assigned_To_Name",
    "Task_Assigned_To_Email",
    "Task_Submitted_On",
    "Task_Submitted_By_DID",
    "Task_Submitted_By_Name",
    "Task_Submitted_By_Email",
    "Task_Approved_On",
    "Task_Approved_By_DID",
    "Task_Approved_By_Name",
    "Task_Approved_By_Email",
    "Task_Cancelled_On",
    "Task_Cancelled_By_DID",
    "Task_Cancelled_By_Name",
    "Task_Cancelled_By_Email",
    "retrieved_at",
]

NUM_COLS = len(EXCEL_COLUMNS)

# Pre-compute column categories by index
DATE_ONLY_COLS = frozenset({
    EXCEL_COLUMNS.index("Task_Scheduled"),
    EXCEL_COLUMNS.index("Task_Submitted_On"),
    EXCEL_COLUMNS.index("Task_Approved_On"),
    EXCEL_COLUMNS.index("Task_Cancelled_On"),
})
DATETIME_COLS = frozenset({
    EXCEL_COLUMNS.index("retrieved_at"),
})
REGULAR_COLS = frozenset(set(range(NUM_COLS)) - DATE_ONLY_COLS - DATETIME_COLS)

QUERY = """
SELECT
    (regexp_match(p.project_name, 'TS(\\d+)'))[1]::int AS source_name,
    at.project_did,
    at.project_status,
    at.asset_did,
    at.asset_id,
    at.asset_name,
    at.asset_requirement_count,
    at.task_did,
    at.task_name,
    at.task_status,
    at.task_scheduled,
    at.task_assigned_to_did,
    at.task_assigned_to_collection,
    at.task_assigned_to_name,
    at.task_assigned_to_email,
    at.task_submitted_on,
    at.task_submitted_by_did,
    at.task_submitted_by_name,
    at.task_submitted_by_email,
    at.task_approved_on,
    at.task_approved_by_did,
    at.task_approved_by_name,
    at.task_approved_by_email,
    at.task_cancelled_on,
    at.task_cancelled_by_did,
    at.task_cancelled_by_name,
    at.task_cancelled_by_email,
    at.loaded_at
FROM data_staging.stg_asset_tasks at
JOIN data_staging.stg_projects p ON at.project_did = p.project_did
WHERE p.project_name = $1
ORDER BY at.asset_id, at.task_name
"""


def write_sheet(worksheet, rows, header_fmt, date_only_fmt, datetime_fmt):
    """Write a single sheet. Optimized with local var caching and skip-None."""
    # Local references to avoid repeated attribute lookups
    ws_write = worksheet.write
    ws_write_dt = worksheet.write_datetime
    regular = REGULAR_COLS
    date_only = DATE_ONLY_COLS
    datetime_cols = DATETIME_COLS
    _ET = ET
    _utc = timezone.utc

    # Write header row
    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        ws_write(0, col_idx, col_name, header_fmt)

    # Write data rows — split by column type to avoid per-cell isinstance checks
    for row_idx, record in enumerate(rows, start=1):
        # Regular columns: write non-None values directly
        for ci in regular:
            val = record[ci]
            if val is not None:
                ws_write(row_idx, ci, val)

        # Date-only columns (date objects from DB)
        for ci in date_only:
            val = record[ci]
            if val is not None:
                ws_write_dt(row_idx, ci, val, date_only_fmt)

        # Datetime columns — convert to Eastern Time
        for ci in datetime_cols:
            val = record[ci]
            if val is not None:
                if val.tzinfo is not None:
                    val = val.astimezone(_ET)
                else:
                    val = val.replace(tzinfo=_utc).astimezone(_ET)
                ws_write_dt(row_idx, ci, val.replace(tzinfo=None), datetime_fmt)

    # Set column widths
    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        worksheet.set_column(col_idx, col_idx, max(len(col_name) + 2, 12))


async def export(output_path: Path):
    load_dotenv(ENV_PATH)
    t_start = time.time()

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
    )
    await conn.execute("SET statement_timeout = '300s'")

    workbook = xlsxwriter.Workbook(str(output_path), {"constant_memory": True})
    header_fmt = workbook.add_format({"bold": True})
    date_only_fmt = workbook.add_format({"num_format": "mm-dd-yy"})
    datetime_fmt = workbook.add_format({"num_format": "m/d/yy h:mm"})

    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_event_loop()
    total_rows = 0

    try:
        # Pipeline: fetch next project while writing current one
        next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[0]))

        for i, project_name in enumerate(PROJECTS):
            ts_label = project_name.split(": ")[1]
            t_q = time.time()

            # Wait for this project's data
            rows = await next_fetch
            t_fetched = time.time()

            # Kick off next project's fetch immediately (overlaps with write)
            if i + 1 < len(PROJECTS):
                next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[i + 1]))

            count = len(rows)
            total_rows += count

            # Write sheet in thread so DB fetch can run concurrently
            worksheet = workbook.add_worksheet(ts_label)
            await loop.run_in_executor(
                executor,
                write_sheet, worksheet, rows, header_fmt, date_only_fmt, datetime_fmt,
            )
            t_written = time.time()

            print(
                f"{ts_label}: {count:>9,} rows  "
                f"(fetch {t_fetched - t_q:.1f}s, write {t_written - t_fetched:.1f}s)"
            )

        # Fetch latest loaded_at and run_id for email metadata
        meta = await conn.fetchrow("""
            SELECT
                MAX(loaded_at) AT TIME ZONE 'America/New_York' AS latest_loaded_at,
                run_id
            FROM data_staging.stg_asset_tasks
            GROUP BY run_id
            ORDER BY MAX(loaded_at) DESC
            LIMIT 1
        """)
        loaded_at = meta["latest_loaded_at"] if meta else None
        run_id = str(meta["run_id"]) if meta else None

    finally:
        workbook.close()
        await conn.close()
        executor.shutdown(wait=False)

    elapsed = time.time() - t_start
    print(f"\nTotal: {total_rows:,} rows across {len(PROJECTS)} tabs in {elapsed:.1f}s")
    if loaded_at:
        print(f"Data loaded at: {loaded_at:%Y-%m-%d %I:%M %p} ET")
        print(f"Run ID: {run_id}")
    print(f"Output: {output_path}")
    return total_rows, loaded_at, run_id


# Google Drive folder name for exports
DRIVE_FOLDER_NAME = "Asset Tasks Exports"
EMAIL_RECIPIENT = "jamil.mendez@ontel.co"


def get_or_create_drive_folder(drive_service, folder_name):
    """Find existing folder by name, or create it. Returns folder ID."""
    result = drive_service.files().list(
        q=f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]

    # Create folder
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive_service.files().create(body=metadata, fields="id").execute()
    print(f"Created Drive folder: {folder_name}")
    return folder["id"]


def upload_to_drive(file_path: Path) -> str:
    """Upload file to Google Drive and return a shareable link."""
    from gmail_client import authenticate_drive
    from googleapiclient.http import MediaFileUpload

    drive_service = authenticate_drive()

    folder_id = get_or_create_drive_folder(drive_service, DRIVE_FOLDER_NAME)

    # Check if file with same name already exists in folder — replace it
    filename = file_path.name
    result = drive_service.files().list(
        q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
        spaces="drive",
        fields="files(id)",
    ).execute()
    existing = result.get("files", [])

    media = MediaFileUpload(str(file_path), resumable=True)

    if existing:
        # Update existing file
        file_id = existing[0]["id"]
        drive_service.files().update(
            fileId=file_id, media_body=media,
        ).execute()
        print(f"Updated existing file in Drive: {filename}")
    else:
        # Create new file
        metadata = {"name": filename, "parents": [folder_id]}
        file_obj = drive_service.files().create(
            body=metadata, media_body=media, fields="id",
        ).execute()
        file_id = file_obj["id"]
        print(f"Uploaded to Drive: {filename}")

    # Set anyone-with-link can view
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    print(f"Shareable link: {link}")
    return link


def send_export_email(file_path: Path, drive_link: str, total_rows: int,
                      loaded_at=None, run_id=None, recipient: str = EMAIL_RECIPIENT):
    """Send email with Google Drive link to the exported file."""
    from gmail_client import authenticate

    service = authenticate()

    today_et = datetime.now(ET).strftime("%B %d, %Y")
    filename = file_path.name
    file_size_mb = file_path.stat().st_size / (1024 * 1024)

    loaded_at_str = loaded_at.strftime("%B %d, %Y %I:%M %p ET") if loaded_at else "Unknown"
    run_id_str = run_id or "Unknown"

    subject = f"Asset Tasks Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Asset Tasks Export</h2>
    <p>The daily asset tasks export is ready.</p>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding: 4px 12px; font-weight: bold;">File</td>
            <td style="padding: 4px 12px;">{filename}</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Total Rows</td>
            <td style="padding: 4px 12px;">{total_rows:,}</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Size</td>
            <td style="padding: 4px 12px;">{file_size_mb:.1f} MB</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Tabs</td>
            <td style="padding: 4px 12px;">TS18, TS17, TS16, TS15, TS14, TS13</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Data Loaded At</td>
            <td style="padding: 4px 12px;">{loaded_at_str}</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Pipeline Run ID</td>
            <td style="padding: 4px 12px;"><code>{run_id_str}</code></td></tr>
    </table>
    <p><a href="{drive_link}" style="display: inline-block; padding: 10px 20px;
        background-color: #1a73e8; color: white; text-decoration: none;
        border-radius: 4px; font-weight: bold;">Download from Google Drive</a></p>
    </body></html>
    """

    msg = MIMEMultipart()
    msg["To"] = recipient
    msg["From"] = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent to {recipient}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Export asset tasks to Excel")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output filename (default: YYYYMMDD.xlsx)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Drive upload and email",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.output:
        output_path = OUTPUT_DIR / args.output
    else:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        output_path = OUTPUT_DIR / f"{today}.xlsx"

    total_rows, loaded_at, run_id = asyncio.run(export(output_path))

    if not args.no_upload:
        try:
            print("\nUploading to Google Drive...")
            t0 = time.time()
            drive_link = upload_to_drive(output_path)
            print(f"Upload took {time.time() - t0:.1f}s")

            print("Sending email notification...")
            send_export_email(output_path, drive_link, total_rows, loaded_at, run_id)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("Excel file was still generated successfully.")


if __name__ == "__main__":
    main()
