"""
Export QA form data from Supabase to Excel — one workbook per TECH-OPS project (TS13-TS18).
Matches the format of the QA_Form_TS*_Data_*.csv sample files.

Each workbook has a single sheet named "Sheet1" with all QA form columns.

Usage:
    python export_qa_form_excel.py
    python export_qa_form_excel.py --no-upload

Output goes to scripts-reference/data_sample/qa_form_exports/ by default.
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
OUTPUT_DIR = SCRIPT_DIR / "data_sample" / "qa_form_exports"

PROJECTS = [
    ("TECH-OPS: TS13", "-NFkG865XjMXlwqZ1AqU"),
    ("TECH-OPS: TS14", "-NV5j_QcTmdwoaGklFvf"),
    ("TECH-OPS: TS15", "-Np5nDzlfJrK_nt5Ro7e"),
    ("TECH-OPS: TS16", "-O99xSQdLiGywc6KRVw-"),
    ("TECH-OPS: TS17", "-ONLJdAstPfeGwVNgpYH"),
    ("TECH-OPS: TS18", "-O_IpQNpLVwhdVC3QYIm"),
]

# (db_column, excel_header) in display order
COLUMNS = [
    ("project",                          "Project"),
    ("site_name",                        "Site Name"),
    ("site_id",                          "Site ID"),
    ("task",                             "Task"),
    ("requirement",                      "Requirement"),
    ("requirement_status",               "Requirement Status"),
    ("live_review_performed",            "Live Review Performed"),
    ("swift_used_for_photos",            "Swift Used for Photos"),
    ("construction_manager",             "Construction Manager (CM)"),
    ("subcontractor",                    "Subcontractor (if applicable)"),
    ("crew_lead",                        "Crew Lead"),
    ("aat",                              "AAT"),
    ("aat_issues",                       "AAT Issues"),
    ("aat_other_issues",                 "AAT (Other issues)"),
    ("ret",                              "RET"),
    ("ret_issues",                       "RET Issues"),
    ("ret_other_issues",                 "RET (Others issues)"),
    ("sweeps",                           "Sweeps"),
    ("sweeps_issues",                    "Sweeps Issues"),
    ("sweeps_other_issues",              "Sweeps (Other issues)"),
    ("pim",                              "PIM"),
    ("pim_issues",                       "PIM Issues"),
    ("pim_other_issues",                 "PIM (Other issues)"),
    ("fiber",                            "Fiber"),
    ("fiber_issues",                     "Fiber Issues"),
    ("fiber_other_issues",               "Fiber (Other issues)"),
    ("pictures",                         "Pictures"),
    ("pictures_issues",                  "Pictures Issues"),
    ("pictures_other_issues",            "Pictures (Other issues)"),
    ("as_builts",                        "As-Builts"),
    ("as_builts_issues",                 "As-Builts Issues"),
    ("as_builts_other_issues",           "As-Builts (Other issues)"),
    ("rf_mitigation",                    "RF Mitigation"),
    ("rf_mitigation_issues",             "RF Mitigation Issues"),
    ("rf_mitigation_other_issues",       "RF Mitigation (Other issues)"),
    ("landlord_tower_owner",             "Landlord / Tower Owner"),
    ("landlord_tower_owner_issues",      "Landlord / Tower Owner Issues"),
    ("permits",                          "Permits"),
    ("additional_documents",             "Additional Documents (if applicable)"),
    ("pmi",                              "PMI (if applicable)"),
    ("pmi_vendor",                       "(PMI) Vendor Antenna Mount Structural Company"),
    ("pmi_others_vendor",                "Others (PMI Vendor)"),
    ("pmi_mount_modification_required",  "(PMI) Mount Modification Required?"),
    ("pmi_issues",                       "PMI Issues"),
    ("pmi_other_issues",                 "PMI (Other issues)"),
    ("pmi_report_received",              "(PMI) Post Modification Inspection Report received?"),
    ("power_testing",                    "Power Testing (if applicable)"),
    ("power_testing_issues",             "Power Testing Issues"),
    ("power_testing_other_issues",       "Power Testing (Other Issues)"),
    ("connectivity_testing",             "Connectivity Testing (if applicable)"),
    ("connectivity_testing_issues",      "Connectivity Testing Issues"),
    ("connectivity_testing_other_issues","Connectivity Testing (Other Issues)"),
    ("optical_power_testing",            "Optical Power Testing (if applicable)"),
    ("optical_power_testing_other_issues","Optical Power Testing (Other Issues)"),
    ("restoration",                      "Restoration (if applicable)"),
    ("na_checklist",                     "NA Checklist (if applicable)"),
    ("na_checklist_issues",              "N/A Checklist Issues"),
    ("na_checklist_other_issues",        "N/A Checklist (Other Issues)"),
    ("rcm_approval",                     "RCM Approval"),
    ("completeness_of_files",            "Completeness of Files"),
    ("sector_photos",                    "Sector Photos"),
    ("powershift_photos",                "Powershift Photos"),
    ("ret_values",                       "RET Values"),
    ("ret_visibility",                   "RET Visibility"),
    ("serials",                          "Serials"),
    ("font_size_of_labels",              "Font Size of Labels"),
    ("labels_sector_tape",               "Labels Sector Tape"),
    ("smart_level",                      "Smart Level"),
    ("calibration_details",              "Calibration Details"),
    ("general_ground",                   "General Ground"),
    ("conditional_pass",                 "Conditional Pass"),
    ("other_landlord_photos",            "Other Landlord Photos"),
    ("signed_pmi_report",                "Signed PMI Report"),
    ("material_packing_signed_pmi",      "Material Packing Signed PMI"),
    ("supports",                         "Supports"),
]

DB_COLS   = [c[0] for c in COLUMNS]
HDR_NAMES = [c[1] for c in COLUMNS]
NUM_COLS  = len(COLUMNS)

PIPELINE_NAME = "forms_extract"


async def check_pipeline_guard(conn):
    """Abort if the latest forms_extract run failed."""
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


QUERY = f"""
SELECT
    {', '.join(f't.{col}' for col in DB_COLS)}
FROM data_staging.stg_qa_form t
WHERE t.project = $1 AND t.run_id = $2
ORDER BY t.site_name, t.task
"""


def make_filename(ts_label: str, project_did: str, export_dt: datetime) -> str:
    """Generate filename: QA_Form_TS{num}_{project_did}_Data_{YYYYMMDD}_{HHMMSS}.xlsx"""
    yyyymmdd = export_dt.strftime("%Y%m%d")
    hhmmss = export_dt.strftime("%H%M%S")
    return f"QA_Form_{ts_label}_{project_did}_Data_{yyyymmdd}_{hhmmss}.xlsx"


def write_workbook(file_path: Path, rows: list):
    """Write a single-sheet workbook for one project. All columns are plain text."""
    workbook  = xlsxwriter.Workbook(str(file_path), {"constant_memory": True})
    header_fmt = workbook.add_format({"bold": True})
    worksheet  = workbook.add_worksheet("Sheet1")
    ws_write   = worksheet.write

    # Header row
    for col_idx, hdr in enumerate(HDR_NAMES):
        ws_write(0, col_idx, hdr, header_fmt)

    # Data rows — all values are text/None, no date conversion needed
    for row_idx, record in enumerate(rows, start=1):
        for ci in range(NUM_COLS):
            val = record[ci]
            if val is not None and val != "":
                ws_write(row_idx, ci, val)

    # Column widths
    for col_idx, hdr in enumerate(HDR_NAMES):
        worksheet.set_column(col_idx, col_idx, max(len(hdr) + 2, 12))

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

    executor  = ThreadPoolExecutor(max_workers=1)
    loop      = asyncio.get_event_loop()
    export_dt = datetime.now(ET)
    results   = []

    try:
        # Pipeline: prefetch next project while writing current one
        next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[0][0], run_id_uuid))

        for i, (project_name, project_did) in enumerate(PROJECTS):
            ts_label = project_name.split(": ")[1]
            t_q      = time.time()

            rows      = await next_fetch
            t_fetched = time.time()

            if i + 1 < len(PROJECTS):
                next_fetch = asyncio.ensure_future(conn.fetch(QUERY, PROJECTS[i + 1][0], run_id_uuid))

            count     = len(rows)
            file_path = output_dir / make_filename(ts_label, project_did, export_dt)

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
            FROM data_staging.stg_qa_form
            WHERE run_id = $1
        """, run_id_uuid)
        loaded_at = meta["latest_loaded_at"] if meta else None
        run_id    = str(run_id_uuid)

    finally:
        await conn.close()
        executor.shutdown(wait=False)

    total_rows = sum(r[2] for r in results)
    elapsed    = time.time() - t_start
    print(f"\nTotal: {total_rows:,} rows across {len(results)} files in {elapsed:.1f}s")
    if loaded_at:
        print(f"Data loaded at: {loaded_at:%Y-%m-%d %I:%M %p} ET")
        print(f"Run ID: {run_id}")

    return results, loaded_at, run_id


# Google Drive folder name for exports
DRIVE_FOLDER_NAME = "QA Form Exports"
EMAIL_RECIPIENTS  = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
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


def upload_file_to_drive(drive_service, folder_id: str, file_path: Path) -> str:
    from googleapiclient.http import MediaFileUpload

    filename = file_path.name
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
        print(f"  Updated in Drive: {filename}")
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file_obj = drive_service.files().create(
            body=metadata, media_body=media, fields="id",
        ).execute()
        file_id  = file_obj["id"]
        print(f"  Uploaded to Drive: {filename}")

    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def upload_all_to_drive(results: list) -> list:
    from gmail_client import authenticate_drive

    drive_service = authenticate_drive()
    folder_id     = get_or_create_drive_folder(drive_service, DRIVE_FOLDER_NAME)

    enriched = []
    for ts_label, file_path, row_count in results:
        link = upload_file_to_drive(drive_service, folder_id, file_path)
        enriched.append((ts_label, file_path, row_count, link))

    return enriched


def send_export_email(enriched_results: list, loaded_at=None, run_id=None, recipients=None):
    from gmail_client import authenticate

    if recipients is None:
        recipients = EMAIL_RECIPIENTS

    service    = authenticate()
    today_et   = datetime.now(ET).strftime("%B %d, %Y")
    total_rows = sum(r[2] for r in enriched_results)
    loaded_at_str = loaded_at.strftime("%B %d, %Y %I:%M %p ET") if loaded_at else "Unknown"
    run_id_str    = run_id or "Unknown"

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

    subject   = f"QA Form Export - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>QA Form Export</h2>
    <p>The daily QA form export is ready — one file per project.</p>
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
    msg["To"]      = ", ".join(recipients)
    msg["From"]    = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Email sent to {', '.join(recipients)}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Export QA form data to Excel (one file per project)")
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
            t0       = time.time()
            enriched = upload_all_to_drive(results)
            print(f"Upload took {time.time() - t0:.1f}s")

            print("Sending email notification...")
            send_export_email(enriched, loaded_at, run_id)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("Excel files were still generated successfully.")


if __name__ == "__main__":
    main()
