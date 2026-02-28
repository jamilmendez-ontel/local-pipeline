"""
Export asset tasks from raw_asset_tasks to a ZIP of CSV files.
Each project is split into 50,000-row chunks, one CSV per chunk.

Filename convention: Ontel_{project_did}_chunk_{start}_{end}.csv
Output ZIP:         scripts-reference/asset_task_extract/YYYYMMDD.zip

Usage:
    python export_asset_tasks_excel.py
    python export_asset_tasks_excel.py --no-upload
"""

import asyncio
import argparse
import base64
import csv
import io
import os
import sys
import time
import zipfile
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv

# Add swift_api_pipeline to path for gmail_client imports
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "swift_api_pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH   = SCRIPT_DIR.parent / "swift_api_pipeline" / ".env"
OUTPUT_DIR = SCRIPT_DIR / "asset_task_extract"

PROJECTS = [
    "TECH-OPS: TS13",
    "TECH-OPS: TS14",
    "TECH-OPS: TS15",
    "TECH-OPS: TS16",
    "TECH-OPS: TS17",
    "TECH-OPS: TS18",
]

# CSV columns in order — names come directly from JSONB keys, plus retrieved_at
CSV_COLUMNS = [
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

CHUNK_SIZE    = 50_000
PIPELINE_NAME = "asset_tasks_extract"

# Query extracts all JSONB fields + retrieved_at, ordered consistently
QUERY = """
SELECT
    data->>'Project_DID'                AS "Project_DID",
    data->>'Project_Status'             AS "Project_Status",
    data->>'Asset_DID'                  AS "Asset_DID",
    data->>'Asset_ID'                   AS "Asset_ID",
    data->>'Asset_Name'                 AS "Asset_Name",
    data->>'Asset_Requirement_Count'    AS "Asset_Requirement_Count",
    data->>'Task_DID'                   AS "Task_DID",
    data->>'Task_Name'                  AS "Task_Name",
    data->>'Task_Status'                AS "Task_Status",
    data->>'Task_Scheduled'             AS "Task_Scheduled",
    data->>'Task_Assigned_To_DID'       AS "Task_Assigned_To_DID",
    data->>'Task_Assigned_To_Collection' AS "Task_Assigned_To_Collection",
    data->>'Task_Assigned_To_Name'      AS "Task_Assigned_To_Name",
    data->>'Task_Assigned_To_Email'     AS "Task_Assigned_To_Email",
    data->>'Task_Submitted_On'          AS "Task_Submitted_On",
    data->>'Task_Submitted_By_DID'      AS "Task_Submitted_By_DID",
    data->>'Task_Submitted_By_Name'     AS "Task_Submitted_By_Name",
    data->>'Task_Submitted_By_Email'    AS "Task_Submitted_By_Email",
    data->>'Task_Approved_On'           AS "Task_Approved_On",
    data->>'Task_Approved_By_DID'       AS "Task_Approved_By_DID",
    data->>'Task_Approved_By_Name'      AS "Task_Approved_By_Name",
    data->>'Task_Approved_By_Email'     AS "Task_Approved_By_Email",
    data->>'Task_Cancelled_On'          AS "Task_Cancelled_On",
    data->>'Task_Cancelled_By_DID'      AS "Task_Cancelled_By_DID",
    data->>'Task_Cancelled_By_Name'     AS "Task_Cancelled_By_Name",
    data->>'Task_Cancelled_By_Email'    AS "Task_Cancelled_By_Email",
    to_char(loaded_at AT TIME ZONE 'America/New_York', 'YYYY-MM-DD HH24:MI:SS') AS retrieved_at
FROM data_raw.raw_asset_tasks
WHERE project_did = $1 AND run_id = $2
ORDER BY data->>'Asset_ID', data->>'Task_Name'
"""


async def check_pipeline_guard(conn):
    """Abort if the latest asset_tasks_extract run failed or any project has missing rows."""
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

    # Verify all 6 projects have rows in stg_asset_tasks
    project_counts = await conn.fetch("""
        SELECT p.project_name, COUNT(at.id) AS row_count
        FROM data_staging.stg_projects p
        LEFT JOIN data_staging.stg_asset_tasks at ON at.project_did = p.project_did
        WHERE p.project_name = ANY($1::text[])
        GROUP BY p.project_name
        ORDER BY p.project_name
    """, PROJECTS)

    missing = [r["project_name"] for r in project_counts if r["row_count"] == 0]
    if missing:
        raise SystemExit(
            f"GUARD FAILED: Projects with 0 rows in stg_asset_tasks: {', '.join(missing)}. "
            f"Aborting export."
        )

    for r in project_counts:
        print(f"  {r['project_name']}: {r['row_count']:,} rows")

    # Verify raw and staging row counts match
    counts = await conn.fetchrow("""
        SELECT
            (SELECT COUNT(*) FROM data_raw.raw_asset_tasks
             WHERE run_id = (
                 SELECT run_id FROM pipeline.pipeline_runs
                 WHERE pipeline_name = 'asset_tasks_extract'
                 ORDER BY started_at DESC LIMIT 1
             )) AS raw_count,
            (SELECT COUNT(*) FROM data_staging.stg_asset_tasks) AS stg_count
    """)
    raw_count = counts["raw_count"]
    stg_count = counts["stg_count"]
    print(f"  Row counts  : raw={raw_count:,} | stg={stg_count:,}")
    if raw_count != stg_count:
        raise SystemExit(
            f"GUARD FAILED: raw_asset_tasks ({raw_count:,}) != stg_asset_tasks ({stg_count:,}). "
            f"Transform may be incomplete. Aborting export."
        )

    print("  Guard passed.\n")


def rows_to_csv_bytes(rows: list) -> bytes:
    """Serialize a list of asyncpg records to CSV bytes (UTF-8 with BOM for Excel compat)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for record in rows:
        writer.writerow([record[col] if record[col] is not None else "" for col in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


async def export(output_dir: Path):
    """Stream raw_asset_tasks per project, chunk into CSVs, write ZIP. Returns (zip_path, summary)."""
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
    await conn.execute("SET statement_timeout = '600s'")
    await check_pipeline_guard(conn)

    # Get the latest run_id and project_dids
    run_row = await conn.fetchrow("""
        SELECT run_id FROM pipeline.pipeline_runs
        WHERE pipeline_name = $1
        ORDER BY started_at DESC LIMIT 1
    """, PIPELINE_NAME)
    run_id = str(run_row["run_id"])

    proj_rows = await conn.fetch("""
        SELECT project_did, project_name
        FROM data_staging.stg_projects
        WHERE project_name = ANY($1::text[])
        ORDER BY project_name
    """, PROJECTS)
    project_map = {r["project_name"]: r["project_did"] for r in proj_rows}

    export_dt  = datetime.now(ET)
    zip_name   = export_dt.strftime("%Y%m%d") + ".zip"
    zip_path   = output_dir / zip_name
    summary    = []   # (project_name, project_did, total_rows, num_chunks)
    total_rows = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for project_name in PROJECTS:
            project_did = project_map[project_name]
            ts_label    = project_name.split(": ")[1]
            t_proj      = time.time()
            proj_rows_count = 0
            chunk_num   = 0
            chunk_start = 1
            pending     = []

            async with conn.transaction():
                cur = await conn.cursor(QUERY, project_did, run_id)
                while True:
                    batch = await cur.fetch(CHUNK_SIZE)
                    if not batch:
                        break
                    pending.extend(batch)
                    proj_rows_count += len(batch)

                    # Flush each full chunk
                    while len(pending) >= CHUNK_SIZE:
                        chunk = pending[:CHUNK_SIZE]
                        pending = pending[CHUNK_SIZE:]
                        chunk_end  = chunk_start + len(chunk) - 1
                        csv_name   = f"Ontel_{project_did}_chunk_{chunk_start}_{chunk_end}.csv"
                        zf.writestr(csv_name, rows_to_csv_bytes(chunk))
                        chunk_start = chunk_end + 1
                        chunk_num  += 1

            # Flush remaining rows as final (partial) chunk
            if pending:
                chunk_end = chunk_start + len(pending) - 1
                csv_name  = f"Ontel_{project_did}_chunk_{chunk_start}_{chunk_end}.csv"
                zf.writestr(csv_name, rows_to_csv_bytes(pending))
                chunk_num += 1

            total_rows += proj_rows_count
            summary.append((project_name, project_did, proj_rows_count, chunk_num))
            print(
                f"{ts_label}: {proj_rows_count:>9,} rows  "
                f"{chunk_num} chunk(s)  ({time.time() - t_proj:.1f}s)"
            )

    await conn.close()

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    elapsed     = time.time() - t_start
    print(f"\nTotal : {total_rows:,} rows | {sum(s[3] for s in summary)} files | {zip_size_mb:.1f} MB")
    print(f"Output: {zip_path}")
    print(f"Time  : {elapsed:.1f}s")

    return zip_path, summary


# Google Drive folder name for exports
DRIVE_FOLDER_NAME = "Asset Tasks Exports"
EMAIL_RECIPIENTS  = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
    "john@ontel.co",
]


def get_or_create_drive_folder(drive_service, folder_name: str) -> str:
    result = drive_service.files().list(
        q=f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    folder   = drive_service.files().create(body=metadata, fields="id").execute()
    print(f"Created Drive folder: {folder_name}")
    return folder["id"]


def upload_to_drive(file_path: Path) -> str:
    """Upload ZIP to Google Drive and return a shareable link."""
    from gmail_client import authenticate_drive
    from googleapiclient.http import MediaFileUpload

    drive_service = authenticate_drive()
    folder_id     = get_or_create_drive_folder(drive_service, DRIVE_FOLDER_NAME)
    filename      = file_path.name

    result   = drive_service.files().list(
        q=f"name = '{filename}' and '{folder_id}' in parents and trashed = false",
        spaces="drive",
        fields="files(id)",
    ).execute()
    existing = result.get("files", [])
    media    = MediaFileUpload(str(file_path), resumable=True)

    if existing:
        file_id = existing[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
        print(f"Updated in Drive: {filename}")
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file_obj = drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id  = file_obj["id"]
        print(f"Uploaded to Drive: {filename}")

    drive_service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"},
    ).execute()

    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    print(f"Shareable link: {link}")
    return link


def send_export_email(zip_path: Path, drive_link: str, summary: list, recipients=None):
    """Send email with Drive link and per-project summary table."""
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service      = authenticate()
    today_et     = datetime.now(ET).strftime("%B %d, %Y")
    total_rows   = sum(s[2] for s in summary)
    total_chunks = sum(s[3] for s in summary)
    zip_size_mb  = zip_path.stat().st_size / (1024 * 1024)

    rows_html = ""
    for project_name, project_did, row_count, num_chunks in summary:
        ts_label = project_name.split(": ")[1]
        rows_html += (
            f"<tr>"
            f"<td style='padding:4px 12px;'>{ts_label}</td>"
            f"<td style='padding:4px 12px; font-family:monospace; font-size:12px;'>{project_did}</td>"
            f"<td style='padding:4px 12px; text-align:right;'>{row_count:,}</td>"
            f"<td style='padding:4px 12px; text-align:right;'>{num_chunks}</td>"
            f"</tr>"
        )

    subject   = f"Asset Tasks Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Asset Tasks Export</h2>
    <p>The daily asset tasks export is ready as a ZIP of CSV files (50,000 rows per file).</p>
    <table style="border-collapse: collapse; margin: 16px 0; border: 1px solid #ddd;">
        <thead>
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 12px; text-align:left; border-bottom:1px solid #ddd;">Project</th>
                <th style="padding:6px 12px; text-align:left; border-bottom:1px solid #ddd;">Project DID</th>
                <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">Rows</th>
                <th style="padding:6px 12px; text-align:right; border-bottom:1px solid #ddd;">CSV Files</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding:4px 12px; font-weight:bold;">ZIP File</td>
            <td style="padding:4px 12px;">{zip_path.name}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Total Rows</td>
            <td style="padding:4px 12px;">{total_rows:,}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Total CSV Files</td>
            <td style="padding:4px 12px;">{total_chunks}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">ZIP Size</td>
            <td style="padding:4px 12px;">{zip_size_mb:.1f} MB</td></tr>
    </table>
    <p><a href="{drive_link}" style="display: inline-block; padding: 10px 20px;
        background-color: #1a73e8; color: white; text-decoration: none;
        border-radius: 4px; font-weight: bold;">Download ZIP from Google Drive</a></p>
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
    parser = argparse.ArgumentParser(
        description="Export asset tasks to ZIP of CSVs (50,000 rows per file)"
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Drive upload and email",
    )
    args = parser.parse_args()

    zip_path, summary = asyncio.run(export(OUTPUT_DIR))

    if not args.no_upload:
        try:
            print("\nUploading to Google Drive...")
            t0         = time.time()
            drive_link = upload_to_drive(zip_path)
            print(f"Upload took {time.time() - t0:.1f}s")

            print("Sending email notification...")
            send_export_email(zip_path, drive_link, summary)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("ZIP file was still generated successfully.")


if __name__ == "__main__":
    main()
