#!/usr/bin/env python3
"""
Timer Discrepancies Pipeline -- Google Form (Sheets) to Supabase

Extracts timer error/discrepancy reports submitted by technicians via a
Google Form. The form responses are stored in a Google Spreadsheet which
we read via the Drive API CSV export.

Columns (from form):
    Timestamp, Email Address, Ontel email, Shift schedule,
    Discrepancy date, Asset name, Task name, Correct duration, Description

Modes:
    Default (incremental): Only inserts rows newer than max(submission_timestamp)
                           in staging. Existing rows are upserted by row_number.
    --full-refresh:        Truncates staging and reloads all rows.

Usage:
    python extract_timer_discrepancies.py                # incremental
    python extract_timer_discrepancies.py --full-refresh  # full refresh
    python extract_timer_discrepancies.py --no-email      # suppress notification
"""

import json
import re
import uuid
import argparse
from datetime import datetime, timezone

from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    get_logger, get_db, close_db, retry_db, setup_logging,
)
from sheets_client import authenticate_sheets, read_spreadsheet
from pipeline_notifier import (
    PipelineResult, PIPELINE_TABLES, capture_logs,
    send_pipeline_email, snapshot_row_counts,
)

logger = get_logger("timer_discrepancies")

SPREADSHEET_ID = "12dBAGHlk-1qHB1p90bD5vnvQkiwkX2-TYhqwkEfEua4"
LOAD_BATCH_SIZE = 500

# Temporary email remaps — applied at normalize time so downstream matching
# (discrepancy -> timer entry) succeeds. These users file discrepancies under
# one address but have their Swift timer activity recorded under another.
# Pending a long-term fix that handles alias accounts properly.
EMAIL_REMAP = {
    "ton@ontel.co": "ronald@ontel.co",
}

# Column header mappings (raw header -> staging column)
_HEADER_MAP = {
    "Timestamp": "submission_timestamp",
    "Email Address": "email_address",
    "What is your Ontel email address?": "ontel_email",
    "What is your shift schedule when the  timer error / discrepancy occured?": "shift_schedule",
    "When did the timer error / discrepancy occur?": "discrepancy_date",
    "What is the asset name where the timer error / discrepancy occurred? ": "asset_name",
    "What is the name of the task where the timer error / discrepancy occurred?": "task_name",
    "What is the correct duration of the task? (in minutes) ": "correct_duration_minutes",
    "When you can, give a short description for the timer error / discrepancy.": "description",
}


# ------------------------------------------------------------------
# Pipeline run tracking
# ------------------------------------------------------------------

def start_pipeline_run(db, run_id: str, metadata: dict = None):
    retry_db(
        lambda: db.execute(
            f"INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs "
            f"(run_id, pipeline_name, status, started_at, metadata) "
            f"VALUES ($1, $2, $3, $4, $5)",
            run_id, "timer_discrepancies", "running",
            datetime.now(timezone.utc), metadata,
        ),
        description="insert pipeline_runs",
    )


def complete_pipeline_run(db, run_id: str, status: str, records: int = None, error: str = None):
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_PIPELINE}.pipeline_runs "
            f"SET status = $1, completed_at = $2, records_extracted = $3, error_message = $4 "
            f"WHERE run_id = $5",
            status, datetime.now(timezone.utc), records, error, run_id,
        ),
        description="update pipeline_runs",
    )


# ------------------------------------------------------------------
# Parse helpers
# ------------------------------------------------------------------

def _parse_timestamp(val: str) -> datetime | None:
    """Parse Google Forms timestamp 'M/D/YYYY H:MM:SS' to UTC datetime.

    Google Forms records timestamps in the form owner's timezone.
    The form owner is in Philippines (UTC+8), so we convert from PHT to UTC.
    """
    if not val or not val.strip():
        return None
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(val.strip(), "%m/%d/%Y %H:%M:%S")
        # Form timestamps are in Philippines time (UTC+8)
        pht = ZoneInfo("Asia/Manila")
        dt_pht = dt.replace(tzinfo=pht)
        return dt_pht.astimezone(timezone.utc)
    except ValueError:
        logger.warning(f"  Failed to parse timestamp: {repr(val)}")
        return None


def _parse_date(val: str):
    """Parse date string 'M/D/YYYY' to date object."""
    if not val or not val.strip():
        return None
    try:
        return datetime.strptime(val.strip(), "%m/%d/%Y").date()
    except ValueError:
        logger.warning(f"  Failed to parse date: {repr(val)}")
        return None


def _parse_duration(val: str) -> int | None:
    """Parse duration value to integer minutes.

    Handles many free-text formats people use in the form:
        '60', '0', '0:00', '1:30', '13 minutes', '5 mins', '2 hrs 50 mins',
        '1 hour and 50 minutes', '60minutes', '300min', '13 hrs.', etc.
    Returns None for truly unparsable values (free text descriptions, N/A, etc.)
    """
    if not val or not val.strip():
        return None
    val = val.strip()

    # Obvious non-duration values
    lower = val.lower().rstrip('.')
    if lower in ('n/a', 'none', 'zero', 'na'):
        return 0 if lower == 'zero' else None
    # "less than 1 min", "<1" -> 1
    if lower.startswith('less than') or lower == '<1':
        return 1

    # H:MM:SS format (e.g. "1:12:33", "00:00:00")
    m = re.match(r'^(\d+):(\d{2}):(\d{2})$', val)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # H:MM format (e.g. "0:00", "1:30")
    m = re.match(r'^(\d+):(\d{2})$', val)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # Compound hours + minutes: "2 hrs 50 mins", "1hr 45min", "2h 10m",
    # "1 hour and 50 minutes", "2hours, 32minutes (152minutes)"
    m = re.match(
        r'^(\d+)\s*(?:hrs?|hours?|h)\s*[,and ]*\s*(\d+)\s*(?:mins?|minutes?|m)',
        val, re.IGNORECASE,
    )
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # Hours only: "3 hours", "13 hrs.", "11 hrs"
    m = re.match(r'^(\d+)\s*(?:hrs?|hours?)\.?$', val, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60

    # "2 hours only"
    m = re.match(r'^(\d+)\s*(?:hrs?|hours?)\s+only$', val, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60

    # Number + minutes/mins/min/minute: "13 minutes", "5 mins", "300min",
    # "60minutes", "540 minuties only", "420 minutes only."
    m = re.match(r'^(\d+)\s*(?:minutes?|mins?|minu?)\.?\s*(?:only)?\.?$', val, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # "X:XX mins" (e.g. "20:00 mins", "10:00 mins") - treat as MM:SS
    m = re.match(r'^(\d+):(\d{2})\s*(?:mins?|minutes?)$', val, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # "NNN minutes (X.X hours)" — number before "minutes"
    m = re.match(r'^(\d+)\s*(?:minutes?|mins?)', val, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # Plain integer or float
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------
# Extract: Google Sheets -> raw + staging
# ------------------------------------------------------------------

def get_max_timestamp(db) -> datetime | None:
    """Get the latest submission_timestamp from staging for incremental sync."""
    row = db.fetchrow(
        f"SELECT MAX(submission_timestamp) as max_ts "
        f"FROM {SCHEMA_STAGING}.stg_timer_discrepancies"
    )
    if row and row["max_ts"]:
        return row["max_ts"]
    return None


def fetch_rows(creds) -> tuple[list[str], list[list[str]]]:
    """Fetch all rows from the timer discrepancies spreadsheet.

    Returns (headers, data_rows) where data_rows excludes the header row.
    """
    logger.info("Fetching spreadsheet data via Drive API...")
    rows = read_spreadsheet(creds, SPREADSHEET_ID)
    if not rows:
        return [], []
    headers = rows[0]
    data_rows = rows[1:]
    logger.info(f"  Fetched {len(data_rows)} data rows ({len(headers)} columns)")
    return headers, data_rows


def load_raw(db, run_id: str, headers: list[str], numbered_rows: list[tuple[int, list[str]]]):
    """Insert raw rows as JSONB into data_raw.raw_timer_discrepancies.

    Args:
        numbered_rows: List of (row_number, row_data) tuples.
    """
    for i in range(0, len(numbered_rows), LOAD_BATCH_SIZE):
        batch = numbered_rows[i:i + LOAD_BATCH_SIZE]
        tuples = []
        for row_num, row in batch:
            data = {}
            for col_idx, val in enumerate(row):
                header = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx}"
                data[header] = val
            tuples.append((run_id, row_num, json.dumps(data)))
        retry_db(
            lambda t=tuples: db.executemany(
                f"INSERT INTO {SCHEMA_RAW}.raw_timer_discrepancies (run_id, row_number, data) "
                f"VALUES ($1, $2, $3::jsonb)",
                t,
            ),
            description=f"insert raw_timer_discrepancies batch {i // LOAD_BATCH_SIZE + 1}",
        )
    logger.info(f"  Loaded {len(numbered_rows)} raw rows")


def transform_row(headers: list[str], row: list[str], row_number: int, run_id: str) -> dict:
    """Parse a single spreadsheet row into a staging dict."""
    # Build raw dict from headers
    raw = {}
    for col_idx, val in enumerate(row):
        header = headers[col_idx] if col_idx < len(headers) else f"col_{col_idx}"
        raw[header] = val.strip() if val else ""

    # Map to staging columns using _HEADER_MAP
    mapped = {}
    for header, col_name in _HEADER_MAP.items():
        mapped[col_name] = raw.get(header, "")

    ontel_email = (mapped.get("ontel_email") or "").lower() or None
    if ontel_email in EMAIL_REMAP:
        ontel_email = EMAIL_REMAP[ontel_email]

    return {
        "submission_timestamp": _parse_timestamp(mapped.get("submission_timestamp", "")),
        "email_address": mapped.get("email_address") or None,
        "ontel_email": ontel_email,
        "shift_schedule": mapped.get("shift_schedule") or None,
        "discrepancy_date": _parse_date(mapped.get("discrepancy_date", "")),
        "asset_name": mapped.get("asset_name") or None,
        "task_name": mapped.get("task_name") or None,
        "correct_duration_minutes": _parse_duration(mapped.get("correct_duration_minutes", "")),
        "description": mapped.get("description") or None,
        "row_number": row_number,
        "run_id": run_id,
    }


def transform_to_staging(
    db,
    run_id: str,
    headers: list[str],
    numbered_rows: list[tuple[int, list[str]]],
    full_refresh: bool = False,
):
    """Parse rows and load into staging.

    Args:
        numbered_rows: List of (row_number, row_data) tuples (already filtered).

    Uses ON CONFLICT (row_number) DO UPDATE for upsert.
    """
    logger.info("Transforming to staging...")

    if full_refresh:
        retry_db(
            lambda: db.execute(
                f"TRUNCATE TABLE {SCHEMA_STAGING}.stg_timer_discrepancies RESTART IDENTITY"
            ),
            description="truncate stg_timer_discrepancies",
        )

    # Parse all rows
    rows = []
    parse_errors = 0
    for row_number, row in numbered_rows:
        try:
            parsed = transform_row(headers, row, row_number, run_id)
            if parsed["submission_timestamp"] is None:
                parse_errors += 1
                logger.warning(f"  Row {row_number}: no valid timestamp, skipping")
                continue
            rows.append(parsed)
        except Exception as e:
            parse_errors += 1
            logger.warning(f"  Row {row_number} parse error: {e}")

    if not rows:
        logger.info("  No new rows to load into staging")
        return 0

    # Upsert SQL
    if full_refresh:
        sql = (
            f"INSERT INTO {SCHEMA_STAGING}.stg_timer_discrepancies "
            f"(submission_timestamp, email_address, ontel_email, shift_schedule, "
            f"discrepancy_date, asset_name, task_name, correct_duration_minutes, "
            f"description, row_number, run_id) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)"
        )
    else:
        sql = (
            f"INSERT INTO {SCHEMA_STAGING}.stg_timer_discrepancies "
            f"(submission_timestamp, email_address, ontel_email, shift_schedule, "
            f"discrepancy_date, asset_name, task_name, correct_duration_minutes, "
            f"description, row_number, run_id) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
            f"ON CONFLICT (row_number) DO UPDATE SET "
            f"submission_timestamp = EXCLUDED.submission_timestamp, "
            f"email_address = EXCLUDED.email_address, "
            f"ontel_email = EXCLUDED.ontel_email, "
            f"shift_schedule = EXCLUDED.shift_schedule, "
            f"discrepancy_date = EXCLUDED.discrepancy_date, "
            f"asset_name = EXCLUDED.asset_name, "
            f"task_name = EXCLUDED.task_name, "
            f"correct_duration_minutes = EXCLUDED.correct_duration_minutes, "
            f"description = EXCLUDED.description, "
            f"run_id = EXCLUDED.run_id, "
            f"loaded_at = NOW()"
        )

    # Batch load
    for i in range(0, len(rows), LOAD_BATCH_SIZE):
        batch = rows[i:i + LOAD_BATCH_SIZE]
        tuples = [
            (
                r["submission_timestamp"], r["email_address"], r["ontel_email"],
                r["shift_schedule"], r["discrepancy_date"], r["asset_name"],
                r["task_name"], r["correct_duration_minutes"], r["description"],
                r["row_number"], r["run_id"],
            )
            for r in batch
        ]
        retry_db(
            lambda t=tuples: db.executemany(sql, t),
            description=f"{'insert' if full_refresh else 'upsert'} stg_timer_discrepancies batch {i // LOAD_BATCH_SIZE + 1}",
        )

    logger.info(
        f"  {'Loaded' if full_refresh else 'Upserted'} {len(rows)} staging rows "
        f"({parse_errors} parse errors)"
    )
    return len(rows)


# ------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------

def _run_pipeline_core(full_refresh: bool = False):
    """Core pipeline logic (called inside log capture wrapper)."""
    db = get_db()
    run_id = str(uuid.uuid4())
    mode = "FULL REFRESH" if full_refresh else "INCREMENTAL"
    logger.info(f"Run ID: {run_id}")

    start_pipeline_run(db, run_id, metadata={"spreadsheet_id": SPREADSHEET_ID, "mode": mode})

    try:
        # 1. Authenticate
        logger.info("Authenticating with Google Drive API...")
        creds = authenticate_sheets()
        logger.info("Authenticated successfully")

        # 2. Determine incremental window
        max_timestamp = None
        if not full_refresh:
            max_timestamp = get_max_timestamp(db)
            if not max_timestamp:
                logger.info("No existing data found -- switching to full refresh")
                full_refresh = True

        # 3. Fetch all rows from spreadsheet
        headers, data_rows = fetch_rows(creds)
        logger.info(f"Total rows fetched: {len(data_rows)}")

        if not data_rows:
            logger.info("No data found in spreadsheet. Nothing to process.")
            complete_pipeline_run(db, run_id, "success", records=0)
            return

        # 4. Filter to new rows only (incremental)
        if not full_refresh and max_timestamp:
            ts_col = headers.index("Timestamp") if "Timestamp" in headers else 0
            new_rows = []
            for i, row in enumerate(data_rows):
                ts = _parse_timestamp(row[ts_col] if ts_col < len(row) else "")
                if ts and ts > max_timestamp:
                    new_rows.append((i + 1, row))  # (row_number, row)
            logger.info(f"  New rows since last run: {len(new_rows)} of {len(data_rows)}")
            if not new_rows:
                logger.info("  No new rows to process")
                complete_pipeline_run(db, run_id, "success", records=0)
                return
        else:
            new_rows = [(i + 1, row) for i, row in enumerate(data_rows)]

        # 5. Load raw (new rows only)
        logger.info("Loading raw data...")
        load_raw(db, run_id, headers, new_rows)

        # 6. Transform to staging
        staging_count = transform_to_staging(
            db, run_id, headers, new_rows,
            full_refresh=full_refresh,
        )

        # 7. Complete
        complete_pipeline_run(db, run_id, "success", records=staging_count)

        logger.info(f"\n{'=' * 60}")
        logger.info("Timer Discrepancies Pipeline Complete")
        logger.info(f"  Raw rows loaded: {len(new_rows)}")
        logger.info(f"  Staging rows {'loaded' if full_refresh else 'upserted'}: {staging_count}")
        logger.info(f"{'=' * 60}\n")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        complete_pipeline_run(db, run_id, "failed", error=str(e)[:500])
        raise


def run_timer_discrepancies_pipeline(full_refresh: bool = False, send_email: bool = True):
    """Main entry point with log capture and email notification."""
    setup_logging()
    run_label = "Timer Discrepancies"

    logger.info(f"\n{'=' * 60}")
    logger.info("Timer Discrepancies Pipeline")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'=' * 60}\n")

    tables = PIPELINE_TABLES.get("Timer Discrepancies")
    started_at = datetime.now(timezone.utc)
    row_counts_before = snapshot_row_counts(tables)

    with capture_logs() as log_handler:
        try:
            _run_pipeline_core(full_refresh=full_refresh)
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)

            if send_email:
                result = PipelineResult(
                    pipeline_name="Timer Discrepancies",
                    status="SUCCESS",
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                )
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="SUCCESS",
                    run_label=run_label,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )

        except Exception as e:
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)

            if send_email:
                result = PipelineResult(
                    pipeline_name="Timer Discrepancies",
                    status="FAILED",
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                    error_message=str(e),
                )
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="FAILED",
                    run_label=run_label,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )
            raise
        finally:
            close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract timer discrepancy data from Google Form")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Truncate staging and reload all rows (default: incremental)")
    parser.add_argument("--no-email", action="store_true",
                        help="Suppress email notification")
    args = parser.parse_args()
    run_timer_discrepancies_pipeline(full_refresh=args.full_refresh, send_email=not args.no_email)
