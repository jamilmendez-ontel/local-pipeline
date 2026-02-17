#!/usr/bin/env python3
"""
AR Aging Pipeline -- Gmail to Supabase

Extracts AR Aging Detail attachments from "Daily Revenue Report" emails,
parses the QuickBooks Excel format, and loads into database.

Each day's file is a snapshot identified by `as_of_date`.
Dedup check: skips emails whose received date already exists in raw_ar_aging
(received date, not as_of_date, because the same as_of_date can appear on consecutive days).

Usage:
    python extract_aging.py                    # Process all unloaded emails
    python extract_aging.py --max-emails 5     # Limit to 5 most recent
    python extract_aging.py --reprocess        # Re-process even if as_of_date exists
"""

import os
import json
import uuid
import tempfile
import argparse
from datetime import datetime, timezone

from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    get_logger, get_db, retry_db
)
from gmail_client import authenticate, search_messages, get_message_details, download_attachment
from parse_aging import parse_aging_excel

logger = get_logger("ar_aging")

LOAD_BATCH_SIZE = 1000
GMAIL_QUERY = 'subject:"Daily Revenue Report" has:attachment'
ATTACHMENT_PATTERN = "Aging+Detail"


def get_existing_received_timestamps(db) -> set:
    """Get all email received timestamps already loaded into raw_ar_aging.
    Uses full timestamp (not just date) so multiple emails on the same day are each loaded.
    """
    rows = db.fetch(
        f'SELECT DISTINCT email_received_date FROM {SCHEMA_RAW}.raw_ar_aging '
        f'WHERE email_received_date IS NOT NULL'
    )
    return {row["email_received_date"].strftime("%Y-%m-%d %H:%M:%S") for row in rows}


def start_pipeline_run(db, run_id: str, metadata: dict = None):
    """Record pipeline run start."""
    retry_db(
        lambda: db.execute(
            f'INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs (run_id, pipeline_name, status, started_at, metadata) '
            f'VALUES ($1, $2, $3, $4, $5)',
            run_id, "ar_aging_extract", "running", datetime.now(timezone.utc), metadata
        ),
        description="insert pipeline_runs"
    )


def complete_pipeline_run(db, run_id: str, status: str, records: int = None, error: str = None):
    """Update pipeline run status."""
    retry_db(
        lambda: db.execute(
            f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
            f'SET status = $1, completed_at = $2, records_extracted = $3, error_message = $4 '
            f'WHERE run_id = $5',
            status, datetime.now(timezone.utc), records, error, run_id
        ),
        description="update pipeline_runs"
    )


def load_raw_batch(db, rows: list):
    """Insert a batch of raw AR aging records."""
    tuples = [
        (row["run_id"], row["as_of_date"], row.get("email_received_date"),
         row.get("source_file"), row["data"])
        for row in rows
    ]
    retry_db(
        lambda: db.executemany(
            f'INSERT INTO {SCHEMA_RAW}.raw_ar_aging (run_id, as_of_date, email_received_date, source_file, data) '
            f'VALUES ($1, $2, $3, $4, $5)',
            tuples
        ),
        description="insert raw_ar_aging"
    )


def transform_ar_aging_for_run(db, run_id: str):
    """Transform raw -> staging for a single run_id (inline transform)."""
    from transform import transform_ar_aging
    transform_ar_aging(db, run_id)


def run_aging_pipeline(max_emails: int = 100, reprocess: bool = False):
    """Main AR Aging pipeline."""
    logger.info(f"\n{'='*60}")
    logger.info(f"AR Aging Extraction Pipeline")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    db = get_db()

    # Get existing received timestamps for dedup
    existing_timestamps = set() if reprocess else get_existing_received_timestamps(db)
    if existing_timestamps:
        logger.info(f"Found {len(existing_timestamps)} existing received timestamps in raw_ar_aging")

    # Gmail authentication
    logger.info("Authenticating with Gmail...")
    service = authenticate()
    logger.info("Gmail authenticated successfully")

    # Search for emails
    logger.info(f"Searching: {GMAIL_QUERY}")
    messages = search_messages(service, GMAIL_QUERY, max_results=max_emails)
    logger.info(f"Found {len(messages)} matching emails")

    if not messages:
        logger.info("No emails found. Nothing to process.")
        return None

    # Process emails (oldest first)
    message_details = []
    for msg in messages:
        details = get_message_details(service, msg["id"])
        message_details.append(details)

    message_details.sort(key=lambda d: d["received_date"])

    total_loaded = 0
    total_skipped = 0
    total_errors = 0
    processed_dates = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for details in message_details:
            msg_id = details["id"]
            received_date = details["received_date"]
            subject = details["subject"]

            logger.info(f"\nProcessing: {subject}")
            logger.info(f"  Received: {received_date:%Y-%m-%d %H:%M:%S UTC}")

            filepath = download_attachment(service, msg_id, ATTACHMENT_PATTERN, tmp_dir)

            if not filepath:
                logger.warning(f"  No '{ATTACHMENT_PATTERN}' attachment found -- skipping")
                total_skipped += 1
                continue

            filename = os.path.basename(filepath)
            logger.info(f"  Downloaded: {filename}")

            try:
                as_of_date, rows = parse_aging_excel(filepath)
            except Exception as e:
                logger.error(f"  Parse error: {e}")
                total_errors += 1
                continue

            logger.info(f"  As of date: {as_of_date}")
            logger.info(f"  Parsed rows: {len(rows)}")

            received_ts_str = received_date.strftime("%Y-%m-%d %H:%M:%S")
            if received_ts_str in existing_timestamps:
                logger.info(f"  SKIPPED -- received timestamp {received_ts_str} already loaded (as_of_date={as_of_date})")
                total_skipped += 1
                continue

            run_id = str(uuid.uuid4())
            logger.info(f"  Run ID: {run_id}")

            start_pipeline_run(db, run_id, metadata={
                "as_of_date": str(as_of_date),
                "source_file": filename,
                "email_received_date": received_date.isoformat(),
                "row_count": len(rows),
            })

            try:
                for i in range(0, len(rows), LOAD_BATCH_SIZE):
                    batch = rows[i:i + LOAD_BATCH_SIZE]
                    raw_rows = [
                        {
                            "run_id": run_id,
                            "as_of_date": as_of_date,
                            "email_received_date": received_date,
                            "source_file": filename,
                            "data": record,
                        }
                        for record in batch
                    ]
                    load_raw_batch(db, raw_rows)

                logger.info(f"  Loaded {len(rows)} raw records")
                total_loaded += len(rows)

                transform_ar_aging_for_run(db, run_id)

                complete_pipeline_run(db, run_id, "success", records=len(rows))
                existing_timestamps.add(received_ts_str)
                processed_dates.append(f"{received_ts_str} (as_of={as_of_date})")

            except Exception as e:
                logger.error(f"  Load/transform error: {e}")
                complete_pipeline_run(db, run_id, "failed", error=str(e))
                total_errors += 1

            try:
                os.remove(filepath)
            except OSError:
                pass

    logger.info(f"\n{'='*60}")
    logger.info(f"AR Aging Pipeline Complete")
    logger.info(f"  Emails processed: {len(message_details)}")
    logger.info(f"  Dates loaded: {len(processed_dates)}")
    logger.info(f"  Total raw records: {total_loaded:,}")
    logger.info(f"  Skipped (dedup): {total_skipped}")
    logger.info(f"  Errors: {total_errors}")
    if processed_dates:
        logger.info(f"  Dates: {', '.join(sorted(processed_dates))}")
    logger.info(f"{'='*60}\n")

    return processed_dates if processed_dates else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract AR Aging from Gmail")
    parser.add_argument("--max-emails", type=int, default=100, help="Maximum emails to process (default: 100)")
    parser.add_argument("--reprocess", action="store_true", help="Re-process even if as_of_date already exists")
    args = parser.parse_args()

    run_aging_pipeline(max_emails=args.max_emails, reprocess=args.reprocess)
