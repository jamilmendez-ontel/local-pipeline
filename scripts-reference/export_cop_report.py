"""
Export COP (Certificate of Performance) report from stg_asset_tasks to Excel.

Generates the "Final COP - Pending Task Report" workbook with 8 sheets:
  1. Summary       — Pivot table + pending LL/PMI lists
  2. 48Hrs Summary — 48Hr completion vs FCOP status analysis
  3. Raw           — Computed join of FCOP approved + LL + PMI data
  4. FCOP approved — All approved Final COP tasks (full 28 cols)
  5. LL            — All LL COP Complete tasks (full 28 cols)
  6. PMI           — All PMI COP Complete tasks (full 28 cols)
  7. 48Hrs         — All 48Hr / Test Package Complete tasks (full 28 cols)
  8. FCOP ongoing  — Final COP tasks that are pending/in_progress (full 28 cols)

Usage:
    python export_cop_report.py
    python export_cop_report.py --output custom_name.xlsx
    python export_cop_report.py --no-upload
    python export_cop_report.py --no-email
"""

import asyncio
import argparse
import base64
import os
import sys
import time
from collections import Counter
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

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------
EXCEL_COLUMNS = [
    "Source.Name", "Project_DID", "Project_Status", "Asset_DID", "Asset_ID",
    "Asset_Name", "Asset_Requirement_Count", "Task_DID", "Task_Name",
    "Task_Status", "Task_Scheduled", "Task_Assigned_To_DID",
    "Task_Assigned_To_Collection", "Task_Assigned_To_Name",
    "Task_Assigned_To_Email", "Task_Submitted_On", "Task_Submitted_By_DID",
    "Task_Submitted_By_Name", "Task_Submitted_By_Email", "Task_Approved_On",
    "Task_Approved_By_DID", "Task_Approved_By_Name", "Task_Approved_By_Email",
    "Task_Cancelled_On", "Task_Cancelled_By_DID", "Task_Cancelled_By_Name",
    "Task_Cancelled_By_Email", "retrieved_at",
]

NUM_COLS = len(EXCEL_COLUMNS)

DATE_ONLY_COLS = frozenset({
    EXCEL_COLUMNS.index("Task_Scheduled"),
    EXCEL_COLUMNS.index("Task_Submitted_On"),
    EXCEL_COLUMNS.index("Task_Approved_On"),
    EXCEL_COLUMNS.index("Task_Cancelled_On"),
})
DATETIME_COLS = frozenset({EXCEL_COLUMNS.index("retrieved_at")})
REGULAR_COLS = frozenset(set(range(NUM_COLS)) - DATE_ONLY_COLS - DATETIME_COLS)

RAW_COLUMNS = [
    "Carrier Group", "Project_DID", "Asset_ID", "Asset_Name",
    "Final COP", "FCOP TA", "LL COP Status", "LL COP",
    "PMI COP Status", "PMI COP", "Days Since Final COP",
]

# Drive / email config
DRIVE_FOLDER_NAME = "COP Reports"
EMAIL_RECIPIENT = "jamil.mendez@ontel.co"

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

PROJECTS_FILTER = """p.project_name IN (
    'TECH-OPS: TS13', 'TECH-OPS: TS14', 'TECH-OPS: TS15',
    'TECH-OPS: TS16', 'TECH-OPS: TS17', 'TECH-OPS: TS18'
)"""

# Base query for 28-column data sheets (reused for each task filter)
DATA_SHEET_QUERY = f"""
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
WHERE {PROJECTS_FILTER}
"""

FCOP_APPROVED_QUERY = DATA_SHEET_QUERY + """
  AND at.task_name IN ('6. Final COP Complete', '7. Final COP Complete', '8. Final COP Complete')
  AND at.task_status = 'approved'
ORDER BY at.asset_id, at.task_name
"""

LL_QUERY = DATA_SHEET_QUERY + """
  AND at.task_name = '1. LL COP Complete'
ORDER BY at.asset_id
"""

PMI_QUERY = DATA_SHEET_QUERY + """
  AND at.task_name = '1. PMI COP Complete'
ORDER BY at.asset_id
"""

HR48_QUERY = DATA_SHEET_QUERY + """
  AND at.task_name = '4. 48Hr / Test Package Complete'
ORDER BY at.asset_id
"""

FCOP_ONGOING_QUERY = DATA_SHEET_QUERY + """
  AND at.task_name IN ('6. Final COP Complete', '7. Final COP Complete', '8. Final COP Complete')
  AND at.task_status NOT IN ('approved', 'cancelled')
ORDER BY at.asset_id, at.task_name
"""

CARRIER_QUERY = """
SELECT asset_did, carrier_group
FROM data_staging.stg_assets
WHERE carrier_group IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_data_sheet(worksheet, rows, header_fmt, date_only_fmt, datetime_fmt):
    """Write a 28-column data sheet (same as export_asset_tasks_excel.py)."""
    _ET = ET
    _utc = timezone.utc

    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        worksheet.write(0, col_idx, col_name, header_fmt)

    for row_idx, record in enumerate(rows, start=1):
        for ci in REGULAR_COLS:
            val = record[ci]
            if val is not None:
                worksheet.write(row_idx, ci, val)
        for ci in DATE_ONLY_COLS:
            val = record[ci]
            if val is not None:
                worksheet.write_datetime(row_idx, ci, val, date_only_fmt)
        for ci in DATETIME_COLS:
            val = record[ci]
            if val is not None:
                if val.tzinfo is not None:
                    val = val.astimezone(_ET)
                else:
                    val = val.replace(tzinfo=_utc).astimezone(_ET)
                worksheet.write_datetime(row_idx, ci, val.replace(tzinfo=None), datetime_fmt)

    for col_idx, col_name in enumerate(EXCEL_COLUMNS):
        worksheet.set_column(col_idx, col_idx, max(len(col_name) + 2, 12))


def build_lookup_by_asset(rows):
    """Build dict keyed by asset_did from data sheet rows (asyncpg Records).

    Data sheet columns by index: 3=asset_did, 9=task_status,
    13=task_assigned_to_name, 15=task_submitted_on.
    First row per asset_did wins (dedup).
    """
    lookup = {}
    for r in rows:
        aid = r[3]  # asset_did is col index 3
        if aid not in lookup:
            lookup[aid] = r
    return lookup


def build_raw_data(fcop_approved_rows, ll_lookup, pmi_lookup, carrier_lookup, today):
    """Build the Raw sheet data by joining FCOP approved with LL/PMI lookups.

    Joins on asset_did (col 3). FCOP approved is 1:1 by asset_did.
    carrier_lookup: dict {asset_did: carrier_group} from stg_assets.
    Returns list of tuples:
      (carrier_group, project_did, asset_id, asset_name, fcop_submitted,
       fcop_ta, ll_status, ll_submitted, pmi_status, pmi_submitted, days_since)
    """
    results = []
    for r in fcop_approved_rows:
        asset_did = r[3]      # asset_did
        project_did = r[1]    # project_did
        asset_id = r[4] or "" # asset_id
        asset_name = r[5] or ""
        fcop_submitted = r[15]  # task_submitted_on
        fcop_ta = r[13] or ""   # task_assigned_to_name

        carrier = carrier_lookup.get(asset_did, "")

        # Lookup LL and PMI by asset_did
        ll_row = ll_lookup.get(asset_did)
        ll_status = (ll_row[9] or "") if ll_row else ""   # task_status
        ll_submitted = ll_row[15] if ll_row else None      # task_submitted_on

        pmi_row = pmi_lookup.get(asset_did)
        pmi_status = (pmi_row[9] or "") if pmi_row else "" # task_status
        pmi_submitted = pmi_row[15] if pmi_row else None    # task_submitted_on

        # Days Since Final COP: if PMI status is pending/in_progress, calc days
        if pmi_status in ("pending", "in_progress") and fcop_submitted is not None:
            days_since = (today - fcop_submitted).days
        else:
            days_since = 0

        results.append((
            carrier, project_did or "", asset_id, asset_name, fcop_submitted,
            fcop_ta, ll_status, ll_submitted, pmi_status, pmi_submitted,
            days_since,
        ))
    return results


def write_raw_sheet(worksheet, raw_data, header_fmt, date_only_fmt):
    """Write the Raw computed sheet."""
    for col_idx, col_name in enumerate(RAW_COLUMNS):
        worksheet.write(0, col_idx, col_name, header_fmt)

    for row_idx, row in enumerate(raw_data, start=1):
        # 0: Carrier Group (str)
        worksheet.write(row_idx, 0, row[0])
        # 1: Project_DID (str)
        worksheet.write(row_idx, 1, row[1])
        # 2: Asset_ID (str)
        worksheet.write(row_idx, 2, row[2])
        # 3: Asset_Name (str)
        worksheet.write(row_idx, 3, row[3])
        # 4: Final COP (date)
        if row[4] is not None:
            worksheet.write_datetime(row_idx, 4, row[4], date_only_fmt)
        # 5: FCOP TA (str)
        worksheet.write(row_idx, 5, row[5])
        # 6: LL COP Status (str)
        worksheet.write(row_idx, 6, row[6])
        # 7: LL COP (date)
        if row[7] is not None:
            worksheet.write_datetime(row_idx, 7, row[7], date_only_fmt)
        # 8: PMI COP Status (str)
        worksheet.write(row_idx, 8, row[8])
        # 9: PMI COP (date)
        if row[9] is not None:
            worksheet.write_datetime(row_idx, 9, row[9], date_only_fmt)
        # 10: Days Since Final COP (int)
        worksheet.write(row_idx, 10, row[10])

    widths = [16, 40, 55, 45, 14, 22, 16, 14, 16, 14, 22]
    for i, w in enumerate(widths):
        worksheet.set_column(i, i, w)


def write_summary_sheet(worksheet, raw_data, header_fmt, date_only_fmt, bold_fmt):
    """Write the Summary sheet with 3 sections side by side."""
    # --- Section 1: Pivot table (cols A-D, starting row 2) ---
    # Header rows
    worksheet.write(2, 0, "Count of Carrier Group", header_fmt)
    worksheet.write(2, 1, "LL COP Status", header_fmt)
    worksheet.write(3, 0, "Carrier Group", header_fmt)
    worksheet.write(3, 1, "approved", header_fmt)
    worksheet.write(3, 2, "cancelled", header_fmt)
    worksheet.write(3, 3, "pending", header_fmt)

    # Build pivot: carrier group -> {ll_status -> count}
    pivot = {}
    for row in raw_data:
        carrier = row[0] or "(blank)"
        ll_status = row[6] or "(blank)"
        if carrier not in pivot:
            pivot[carrier] = Counter()
        pivot[carrier][ll_status] += 1

    # Write pivot rows in order: AT&T/DISH, TMO/USCC, Verizon
    carrier_order = ["AT&T/DISH", "TMO/USCC", "Verizon"]
    pivot_row = 4
    for carrier in carrier_order:
        counts = pivot.get(carrier, Counter())
        worksheet.write(pivot_row, 0, carrier)
        worksheet.write(pivot_row, 1, counts.get("approved", 0))
        worksheet.write(pivot_row, 2, counts.get("cancelled", 0))
        worksheet.write(pivot_row, 3, counts.get("pending", 0))
        pivot_row += 1

    worksheet.set_column(0, 0, 18)
    worksheet.set_column(1, 3, 12)

    # --- Section 2: Pending & In-Progress LL COPs (cols Q-V, starting row 1) ---
    ll_start_col = 16  # Q
    worksheet.merge_range(1, ll_start_col, 1, ll_start_col + 5,
                          "Pending & In-Progress LL COPs", bold_fmt)
    ll_headers = ["Asset_Name", "Asset_ID", "Carrier Group", "Final COP",
                  "LL COP Status", "FCOP TA"]
    for i, h in enumerate(ll_headers):
        worksheet.write(2, ll_start_col + i, h, header_fmt)

    ll_rows = [r for r in raw_data if r[6] in ("pending", "in_progress")]
    for ri, row in enumerate(ll_rows, start=3):
        worksheet.write(ri, ll_start_col, row[3])      # Asset_Name
        worksheet.write(ri, ll_start_col + 1, row[2])   # Asset_ID
        worksheet.write(ri, ll_start_col + 2, row[0])   # Carrier Group
        if row[4] is not None:
            worksheet.write_datetime(ri, ll_start_col + 3, row[4], date_only_fmt)
        worksheet.write(ri, ll_start_col + 4, row[6])   # LL COP Status
        worksheet.write(ri, ll_start_col + 5, row[5])   # FCOP TA

    worksheet.set_column(ll_start_col, ll_start_col, 40)
    worksheet.set_column(ll_start_col + 1, ll_start_col + 1, 55)
    worksheet.set_column(ll_start_col + 2, ll_start_col + 2, 16)
    worksheet.set_column(ll_start_col + 3, ll_start_col + 3, 14)
    worksheet.set_column(ll_start_col + 4, ll_start_col + 4, 16)
    worksheet.set_column(ll_start_col + 5, ll_start_col + 5, 22)

    # --- Section 3: Pending & In-Progress PMI COPs >= 14 days (cols X-AD) ---
    pmi_start_col = 23  # X
    worksheet.merge_range(1, pmi_start_col, 1, pmi_start_col + 6,
                          "Pending & In-Progress PMI COPs (>= 14 Days after FCOP)", bold_fmt)
    pmi_headers = ["Asset_Name", "Asset_ID", "Carrier Group", "Final COP",
                   "PMI COP Status", "Days Since Final COP", "FCOP TA"]
    for i, h in enumerate(pmi_headers):
        worksheet.write(2, pmi_start_col + i, h, header_fmt)

    pmi_rows = [r for r in raw_data
                if r[8] in ("pending", "in_progress") and r[10] >= 14]
    for ri, row in enumerate(pmi_rows, start=3):
        worksheet.write(ri, pmi_start_col, row[3])       # Asset_Name
        worksheet.write(ri, pmi_start_col + 1, row[2])    # Asset_ID
        worksheet.write(ri, pmi_start_col + 2, row[0])    # Carrier Group
        if row[4] is not None:
            worksheet.write_datetime(ri, pmi_start_col + 3, row[4], date_only_fmt)
        worksheet.write(ri, pmi_start_col + 4, row[8])    # PMI COP Status
        worksheet.write(ri, pmi_start_col + 5, row[10])   # Days Since Final COP
        worksheet.write(ri, pmi_start_col + 6, row[5])    # FCOP TA

    worksheet.set_column(pmi_start_col, pmi_start_col, 40)
    worksheet.set_column(pmi_start_col + 1, pmi_start_col + 1, 55)
    worksheet.set_column(pmi_start_col + 2, pmi_start_col + 2, 16)
    worksheet.set_column(pmi_start_col + 3, pmi_start_col + 3, 14)
    worksheet.set_column(pmi_start_col + 4, pmi_start_col + 4, 16)
    worksheet.set_column(pmi_start_col + 5, pmi_start_col + 5, 22)
    worksheet.set_column(pmi_start_col + 6, pmi_start_col + 6, 22)

    print(f"  Summary: pivot {len(carrier_order)} carriers, "
          f"{len(ll_rows)} pending LL, {len(pmi_rows)} pending PMI (>=14d)")


def write_48hrs_summary_sheet(worksheet, fcop_ongoing_rows, hr48_lookup,
                              header_fmt, date_only_fmt, bold_fmt):
    """Write the 48Hrs Summary sheet with 3 side-by-side sections.

    Uses FCOP ongoing rows (pending/in_progress) + hr48 lookup dict.
    Section 3 iterates hr48_lookup directly for all approved 48Hr tasks.
    """
    section1 = []  # 48Hr approved + FCOP pending/in_progress
    section2 = []  # FCOP pending/in_progress
    section3 = []  # 48Hr approved + submitted >= 2025

    cutoff_2025 = date(2025, 1, 1)

    # Sections 1 & 2: iterate FCOP ongoing (already filtered to pending/in_progress)
    for r in fcop_ongoing_rows:
        asset_name = r[5] or ""    # asset_name
        fcop_status = r[9] or ""   # task_status
        asset_did = r[3]           # asset_did

        section2.append((asset_name, fcop_status))

        hr48_row = hr48_lookup.get(asset_did)
        if hr48_row and (hr48_row[9] or "") == "approved":
            section1.append((asset_name, "approved", fcop_status))

    # Section 3: iterate all 48Hr rows for approved + submitted >= 2025
    for aid, hr48_row in hr48_lookup.items():
        hr48_status = hr48_row[9] or ""
        hr48_submitted = hr48_row[15]  # task_submitted_on
        if (hr48_status == "approved" and hr48_submitted is not None
                and hr48_submitted >= cutoff_2025):
            section3.append((hr48_row[5] or "", hr48_status, hr48_submitted))
    section3.sort(key=lambda x: x[0])

    # --- Section 1: 48-Hour Completed Sites with Ongoing Final COP (cols B-D) ---
    s1_col = 1  # B
    worksheet.merge_range(1, s1_col, 1, s1_col + 2,
                          "48-Hour Completed Sites with Ongoing Final COP", bold_fmt)
    s1_headers = ["Asset_Name", "48Hrs COP Status", "Final COP Status"]
    for i, h in enumerate(s1_headers):
        worksheet.write(2, s1_col + i, h, header_fmt)
    for ri, row in enumerate(section1, start=3):
        worksheet.write(ri, s1_col, row[0])
        worksheet.write(ri, s1_col + 1, row[1])
        worksheet.write(ri, s1_col + 2, row[2])

    worksheet.set_column(s1_col, s1_col, 45)
    worksheet.set_column(s1_col + 1, s1_col + 2, 18)

    # --- Section 2: Pending and In Progress FCOP (cols F-G) ---
    s2_col = 5  # F
    worksheet.merge_range(1, s2_col, 1, s2_col + 1,
                          "Pending and In Progress FCOP", bold_fmt)
    s2_headers = ["Asset_Name", "Task_Status"]
    for i, h in enumerate(s2_headers):
        worksheet.write(2, s2_col + i, h, header_fmt)
    for ri, row in enumerate(section2, start=3):
        worksheet.write(ri, s2_col, row[0])
        worksheet.write(ri, s2_col + 1, row[1])

    worksheet.set_column(s2_col, s2_col, 45)
    worksheet.set_column(s2_col + 1, s2_col + 1, 14)

    # --- Section 3: 48Hrs COP Complete Submitted 2025 onwards (cols I-K) ---
    s3_col = 8  # I
    worksheet.merge_range(1, s3_col, 1, s3_col + 2,
                          "48Hrs COP Complete Submitted on 2025 onwards", bold_fmt)
    s3_headers = ["Asset_Name", "Task_Status", "Task_Submitted_On"]
    for i, h in enumerate(s3_headers):
        worksheet.write(2, s3_col + i, h, header_fmt)
    for ri, row in enumerate(section3, start=3):
        worksheet.write(ri, s3_col, row[0])
        worksheet.write(ri, s3_col + 1, row[1])
        if row[2] is not None:
            worksheet.write_datetime(ri, s3_col + 2, row[2], date_only_fmt)

    worksheet.set_column(s3_col, s3_col, 45)
    worksheet.set_column(s3_col + 1, s3_col + 1, 14)
    worksheet.set_column(s3_col + 2, s3_col + 2, 18)

    print(f"  48Hrs Summary: {len(section1)} completed+ongoing, "
          f"{len(section2)} pending FCOP, {len(section3)} submitted 2025+")


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

async def _fetch_with_timeout(pool, query, label):
    """Fetch query results using a connection from the pool."""
    async with pool.acquire() as conn:
        await conn.execute("SET statement_timeout = '300s'")
        t0 = time.time()
        rows = await conn.fetch(query)
        print(f"  {label}: {len(rows):>9,} rows ({time.time() - t0:.1f}s)")
        return rows


async def export(output_path: Path):
    load_dotenv(ENV_PATH)
    t_start = time.time()

    pool = await asyncpg.create_pool(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
        min_size=3,
        max_size=7,
    )

    print("Fetching data...")
    t_q = time.time()

    # Run all 6 queries in parallel using pool connections
    (fcop_approved, ll_rows, pmi_rows, hr48_rows, fcop_ongoing,
     carrier_rows) = await asyncio.gather(
        _fetch_with_timeout(pool, FCOP_APPROVED_QUERY, "FCOP approved"),
        _fetch_with_timeout(pool, LL_QUERY, "LL"),
        _fetch_with_timeout(pool, PMI_QUERY, "PMI"),
        _fetch_with_timeout(pool, HR48_QUERY, "48Hrs"),
        _fetch_with_timeout(pool, FCOP_ONGOING_QUERY, "FCOP ongoing"),
        _fetch_with_timeout(pool, CARRIER_QUERY, "Carrier groups"),
    )
    print(f"All queries completed in {time.time() - t_q:.1f}s")

    await pool.close()

    # Build lookup dicts for computed sheets (keyed by asset_did)
    carrier_lookup = {r[0]: r[1] for r in carrier_rows}  # asset_did -> carrier_group
    ll_lookup = build_lookup_by_asset(ll_rows)
    pmi_lookup = build_lookup_by_asset(pmi_rows)
    hr48_lookup = build_lookup_by_asset(hr48_rows)

    # Build Raw sheet data in Python (joins FCOP approved with LL/PMI + carrier)
    today = date.today()
    raw_data = build_raw_data(fcop_approved, ll_lookup, pmi_lookup, carrier_lookup, today)

    # Write Excel workbook (no constant_memory — summary sheets write to
    # non-sequential rows across side-by-side sections)
    workbook = xlsxwriter.Workbook(str(output_path))
    header_fmt = workbook.add_format({"bold": True})
    bold_fmt = workbook.add_format({"bold": True, "font_size": 11})
    date_only_fmt = workbook.add_format({"num_format": "mm-dd-yy"})
    datetime_fmt = workbook.add_format({"num_format": "m/d/yy h:mm"})

    total_rows = 0

    # Sheet 1: Summary (small, computed)
    print("Writing Summary...")
    ws_summary = workbook.add_worksheet("Summary")
    write_summary_sheet(ws_summary, raw_data, header_fmt, date_only_fmt, bold_fmt)

    # Sheet 2: 48Hrs Summary (computed from FCOP ongoing + 48Hr data)
    print("Writing 48Hrs Summary...")
    ws_48_summary = workbook.add_worksheet("48Hrs Summary")
    write_48hrs_summary_sheet(ws_48_summary, fcop_ongoing, hr48_lookup,
                              header_fmt, date_only_fmt, bold_fmt)

    # Sheet 3: Raw (computed)
    print("Writing Raw...")
    ws_raw = workbook.add_worksheet("Raw")
    write_raw_sheet(ws_raw, raw_data, header_fmt, date_only_fmt)
    total_rows += len(raw_data)

    # Sheet 4-8: Data sheets (28 columns each)
    data_sheets = [
        ("FCOP approved", fcop_approved),
        ("LL", ll_rows),
        ("PMI", pmi_rows),
        ("48Hrs", hr48_rows),
        ("FCOP ongoing", fcop_ongoing),
    ]

    for sheet_name, rows in data_sheets:
        t_w = time.time()
        ws = workbook.add_worksheet(sheet_name)
        write_data_sheet(ws, rows, header_fmt, date_only_fmt, datetime_fmt)
        total_rows += len(rows)
        print(f"  {sheet_name}: {len(rows):>9,} rows ({time.time() - t_w:.1f}s)")

    workbook.close()
    elapsed = time.time() - t_start
    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nTotal: {total_rows:,} data rows across 8 sheets in {elapsed:.1f}s")
    print(f"Output: {output_path} ({file_size_mb:.1f} MB)")
    return total_rows


# ---------------------------------------------------------------------------
# Google Drive upload + email (reuse pattern from export_asset_tasks_excel.py)
# ---------------------------------------------------------------------------

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
        print(f"Updated existing file in Drive: {filename}")
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        file_obj = drive_service.files().create(
            body=metadata, media_body=media, fields="id",
        ).execute()
        file_id = file_obj["id"]
        print(f"Uploaded to Drive: {filename}")

    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    print(f"Shareable link: {link}")
    return link


def send_export_email(file_path: Path, drive_link: str, total_rows: int,
                      recipient: str = EMAIL_RECIPIENT):
    """Send email with Google Drive link to the exported file."""
    from gmail_client import authenticate

    service = authenticate()
    today_et = datetime.now(ET).strftime("%B %d, %Y")
    filename = file_path.name
    file_size_mb = file_path.stat().st_size / (1024 * 1024)

    subject = f"Final COP - Pending Task Report - {today_et}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2>Final COP - Pending Task Report</h2>
    <p>The weekly COP report is ready.</p>
    <table style="border-collapse: collapse; margin: 16px 0;">
        <tr><td style="padding: 4px 12px; font-weight: bold;">File</td>
            <td style="padding: 4px 12px;">{filename}</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Total Data Rows</td>
            <td style="padding: 4px 12px;">{total_rows:,}</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Size</td>
            <td style="padding: 4px 12px;">{file_size_mb:.1f} MB</td></tr>
        <tr><td style="padding: 4px 12px; font-weight: bold;">Sheets</td>
            <td style="padding: 4px 12px;">Summary, 48Hrs Summary, Raw, FCOP approved, LL, PMI, 48Hrs, FCOP ongoing</td></tr>
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export Final COP - Pending Task Report to Excel")
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output filename (default: Final COP - Pending Task Report - YYYYMMDD.xlsx)",
    )
    parser.add_argument(
        "--no-upload", action="store_true",
        help="Skip Google Drive upload",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Skip email notification",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.output:
        output_path = OUTPUT_DIR / args.output
    else:
        today = datetime.now(ET).strftime("%Y%m%d")
        output_path = OUTPUT_DIR / f"Final COP - Pending Task Report - {today}.xlsx"

    total_rows = asyncio.run(export(output_path))

    if not args.no_upload:
        try:
            print("\nUploading to Google Drive...")
            t0 = time.time()
            drive_link = upload_to_drive(output_path)
            print(f"Upload took {time.time() - t0:.1f}s")

            if not args.no_email:
                print("Sending email notification...")
                send_export_email(output_path, drive_link, total_rows)
        except Exception as e:
            print(f"ERROR: Drive upload/email failed: {e}")
            print("Excel file was still generated successfully.")
    elif not args.no_email:
        print("Skipping email (no Drive link available without upload)")


if __name__ == "__main__":
    main()
