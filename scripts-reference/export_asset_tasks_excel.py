"""
Export asset tasks from raw_asset_tasks to two ZIP files:
  1. CSV ZIP  — 50K-row chunks, attached to email (~21 MB)
  2. XLSX ZIP — single workbook with one sheet per project, uploaded to Drive (~168 MB)

Output:
    scripts-reference/asset_task_extract/YYYYMMDD.zip   (CSV chunks)
    scripts-reference/YYYYMMDD.zip                      (XLSX workbook)

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
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv
from xlsxwriter import Workbook

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


def build_xlsx_zip(project_data: dict, xlsx_zip_path: Path):
    """Write a single XLSX workbook (one sheet per project) into a ZIP file.

    Args:
        project_data: {ts_label: [[col_values], ...], ...} — all rows per project
        xlsx_zip_path: Output path for the ZIP containing the XLSX
    """
    t0 = time.time()
    xlsx_name = xlsx_zip_path.stem + ".xlsx"
    xlsx_tmp  = xlsx_zip_path.parent / xlsx_name

    wb = Workbook(str(xlsx_tmp), {"constant_memory": True})
    for ts_label, rows in project_data.items():
        ws = wb.add_worksheet(ts_label)
        # Header row
        for col_idx, col_name in enumerate(CSV_COLUMNS):
            ws.write(0, col_idx, col_name)
        # Data rows
        for row_idx, row in enumerate(rows, start=1):
            for col_idx, val in enumerate(row):
                ws.write(row_idx, col_idx, val)
    wb.close()

    with zipfile.ZipFile(xlsx_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(xlsx_tmp, xlsx_name)
    xlsx_tmp.unlink()

    size_mb = xlsx_zip_path.stat().st_size / (1024 * 1024)
    print(f"XLSX ZIP: {xlsx_zip_path.name} ({size_mb:.1f} MB, {time.time() - t0:.1f}s)")
    return xlsx_zip_path


async def export(output_dir: Path):
    """Stream raw_asset_tasks per project, build CSV ZIP + XLSX ZIP.

    Returns (csv_zip_path, xlsx_zip_path, summary).
    """
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
    date_str   = export_dt.strftime("%Y%m%d")
    csv_zip_path  = output_dir / (date_str + ".zip")
    xlsx_zip_path = SCRIPT_DIR / (date_str + ".zip")
    summary    = []   # (project_name, project_did, total_rows, num_chunks)
    total_rows = 0
    # Collect rows per project for XLSX generation
    project_data = {}  # {ts_label: [[col_values], ...]}

    with zipfile.ZipFile(csv_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for project_name in PROJECTS:
            project_did = project_map[project_name]
            ts_label    = project_name.split(": ")[1]
            t_proj      = time.time()
            proj_rows_count = 0
            chunk_num   = 0
            chunk_start = 1
            pending     = []
            xlsx_rows   = []

            async with conn.transaction():
                cur = await conn.cursor(QUERY, project_did, run_id)
                while True:
                    batch = await cur.fetch(CHUNK_SIZE)
                    if not batch:
                        break
                    pending.extend(batch)
                    proj_rows_count += len(batch)

                    # Collect for XLSX
                    for record in batch:
                        xlsx_rows.append([
                            record[col] if record[col] is not None else ""
                            for col in CSV_COLUMNS
                        ])

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

            project_data[ts_label] = xlsx_rows
            total_rows += proj_rows_count
            summary.append((project_name, project_did, proj_rows_count, chunk_num))
            print(
                f"{ts_label}: {proj_rows_count:>9,} rows  "
                f"{chunk_num} chunk(s)  ({time.time() - t_proj:.1f}s)"
            )

    await conn.close()

    csv_size_mb = csv_zip_path.stat().st_size / (1024 * 1024)
    elapsed     = time.time() - t_start
    print(f"\nTotal : {total_rows:,} rows | {sum(s[3] for s in summary)} files | {csv_size_mb:.1f} MB")
    print(f"CSV ZIP : {csv_zip_path}")
    print(f"Time  : {elapsed:.1f}s")

    # Build XLSX workbook ZIP
    print("\nBuilding XLSX workbook...")
    build_xlsx_zip(project_data, xlsx_zip_path)

    return csv_zip_path, xlsx_zip_path, summary


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


def send_export_email(
    csv_drive_link: str | None,
    xlsx_drive_link: str | None,
    summary: list,
    csv_size_mb: float,
    xlsx_size_mb: float,
    recipients=None,
):
    """Send lightweight email with Google Drive links for both ZIP files."""
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service      = authenticate()
    today_et     = datetime.now(ET).strftime("%B %d, %Y")
    total_rows   = sum(s[2] for s in summary)
    total_chunks = sum(s[3] for s in summary)

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

    # Build download buttons
    btn_style = ("display:inline-block; padding:10px 20px; color:white; "
                 "text-decoration:none; border-radius:4px; font-weight:bold; margin-right:12px;")

    xlsx_btn = ""
    if xlsx_drive_link:
        xlsx_btn = (
            f'<a href="{xlsx_drive_link}" style="{btn_style} background-color:#1a73e8;">'
            f'Download XLSX Workbook ({xlsx_size_mb:.0f} MB)</a>'
        )

    csv_btn = ""
    if csv_drive_link:
        csv_btn = (
            f'<a href="{csv_drive_link}" style="{btn_style} background-color:#34a853;">'
            f'Download CSV ZIP ({csv_size_mb:.0f} MB)</a>'
        )

    subject   = f"Asset Tasks Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Asset Tasks Export</h2>
    <p>The daily asset tasks export is ready.</p>
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
        <tr><td style="padding:4px 12px; font-weight:bold;">Total Rows</td>
            <td style="padding:4px 12px;">{total_rows:,}</td></tr>
        <tr><td style="padding:4px 12px; font-weight:bold;">Total CSV Files</td>
            <td style="padding:4px 12px;">{total_chunks}</td></tr>
    </table>
    <h3>Download from Google Drive</h3>
    <p style="margin: 16px 0;">
        {xlsx_btn}
        {csv_btn}
    </p>
    <ul style="color:#555; font-size:13px;">
        <li><strong>XLSX Workbook</strong> — single Excel file with one sheet per project</li>
        <li><strong>CSV ZIP</strong> — bulk CSV files split into {CHUNK_SIZE:,}-row chunks</li>
    </ul>
    </body></html>
    """

    msg = MIMEText(html_body, "html")
    msg["To"]      = ", ".join(recipients)
    msg["From"]    = "me"
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent to {', '.join(recipients)}: {subject}")


def main():
    parser = argparse.ArgumentParser(
        description="Export asset tasks to CSV ZIP + XLSX ZIP"
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Drive upload and email",
    )
    args = parser.parse_args()

    csv_zip_path, xlsx_zip_path, summary = asyncio.run(export(OUTPUT_DIR))
    csv_size_mb  = csv_zip_path.stat().st_size / (1024 * 1024)
    xlsx_size_mb = xlsx_zip_path.stat().st_size / (1024 * 1024)

    if not args.no_upload:
        xlsx_drive_link = None
        csv_drive_link  = None

        # Step 1: Upload XLSX to Drive
        try:
            print("\nUploading XLSX ZIP to Google Drive...")
            t0 = time.time()
            xlsx_drive_link = upload_to_drive(xlsx_zip_path)
            print(f"Upload took {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"ERROR: XLSX Drive upload failed: {e}")

        # Step 2: Upload CSV ZIP to Drive
        try:
            print("Uploading CSV ZIP to Google Drive...")
            t0 = time.time()
            csv_drive_link = upload_to_drive(csv_zip_path)
            print(f"Upload took {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"ERROR: CSV Drive upload failed: {e}")

        # Step 3: Send lightweight email with Drive links (no attachment)
        if xlsx_drive_link or csv_drive_link:
            try:
                print("Sending email notification...")
                send_export_email(
                    csv_drive_link, xlsx_drive_link, summary,
                    csv_size_mb, xlsx_size_mb,
                )
            except Exception as e:
                print(f"ERROR: Email send failed: {e}")
        else:
            print("ERROR: Both Drive uploads failed. Skipping email.")
            print("ZIP files were still generated successfully.")


if __name__ == "__main__":
    main()
