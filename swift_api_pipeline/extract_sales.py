#!/usr/bin/env python3
"""
Sales by Product/Service Pipeline — Gmail to Supabase

Extracts Sales by Product/Service Detail attachments from "Daily Revenue Report" emails,
parses the QuickBooks Excel format, and loads into Supabase.

Each day's file is a snapshot identified by `as_of_date`.
Dedup check: skips emails whose `as_of_date` already exists in raw_sales_detail.

Usage:
    python extract_sales.py                    # Process all unloaded emails
    python extract_sales.py --max-emails 5     # Limit to 5 most recent
    python extract_sales.py --reprocess        # Re-process even if as_of_date exists
"""

import os
import uuid
import tempfile
import argparse
from datetime import datetime, timezone

from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    get_logger, create_supabase_client, retry_supabase
)
from gmail_client import authenticate, search_messages, get_message_details, download_attachment
from parse_sales import parse_sales_excel

logger = get_logger("sales_detail")

LOAD_BATCH_SIZE = 1000
GMAIL_QUERY = 'subject:"Daily Revenue Report" has:attachment'
ATTACHMENT_PATTERN = "Sales+by++ProductService"


def get_existing_as_of_dates(client) -> set:
    """Get all as_of_date values already loaded into raw_sales_detail."""
    results = []
    offset = 0
    batch_size = 1000

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_sales_detail").select(
            "as_of_date"
        ).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        results.extend(result.data)
        if len(result.data) < batch_size:
            break
        offset += batch_size

    return {row["as_of_date"] for row in results}


def start_pipeline_run(client, run_id: str, metadata: dict = None):
    """Record pipeline run start."""
    row = {
        "run_id": run_id,
        "pipeline_name": "sales_detail_extract",
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        row["metadata"] = metadata

    retry_supabase(
        lambda: client.schema(SCHEMA_PIPELINE).table("pipeline_runs").insert(row).execute(),
        description="insert pipeline_runs"
    )


def complete_pipeline_run(client, run_id: str, status: str, records: int = None, error: str = None):
    """Update pipeline run status."""
    update_data = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if records is not None:
        update_data["records_extracted"] = records
    if error:
        update_data["error_message"] = error

    retry_supabase(
        lambda: client.schema(SCHEMA_PIPELINE).table("pipeline_runs").update(
            update_data
        ).eq("run_id", run_id).execute(),
        description="update pipeline_runs"
    )


def load_raw_batch(client, rows: list):
    """Insert a batch of raw sales detail records."""
    retry_supabase(
        lambda: client.schema(SCHEMA_RAW).table("raw_sales_detail").insert(rows).execute(),
        description="insert raw_sales_detail"
    )


def transform_sales_for_run(client, run_id: str):
    """Transform raw → staging for a single run_id (inline transform)."""
    from transform import transform_sales_detail
    transform_sales_detail(client, run_id)


def run_sales_pipeline(max_emails: int = 100, reprocess: bool = False):
    """
    Main Sales by Product/Service pipeline.

    1. Authenticate with Gmail
    2. Search for Daily Revenue Report emails
    3. For each email, download Sales+by++ProductService attachment
    4. Parse Excel → extract as_of_date + rows
    5. Dedup check (skip if as_of_date already loaded)
    6. Load raw records in batches
    7. Transform raw → staging
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Sales by Product/Service Extraction Pipeline")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    # Supabase client for this pipeline
    client = create_supabase_client()

    # Get existing as_of_dates for dedup
    existing_dates = set() if reprocess else get_existing_as_of_dates(client)
    if existing_dates:
        logger.info(f"Found {len(existing_dates)} existing as_of_dates in raw_sales_detail")

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

    # Process emails (oldest first for chronological loading)
    message_details = []
    for msg in messages:
        details = get_message_details(service, msg["id"])
        message_details.append(details)

    message_details.sort(key=lambda d: d["received_date"])

    total_loaded = 0
    total_skipped = 0
    total_errors = 0
    processed_dates = []

    # Temp directory for downloads
    with tempfile.TemporaryDirectory() as tmp_dir:
        for details in message_details:
            msg_id = details["id"]
            received_date = details["received_date"]
            subject = details["subject"]

            logger.info(f"\nProcessing: {subject}")
            logger.info(f"  Received: {received_date:%Y-%m-%d %H:%M:%S UTC}")

            # Download Sales+by++ProductService attachment
            filepath = download_attachment(
                service, msg_id, ATTACHMENT_PATTERN, tmp_dir
            )

            if not filepath:
                logger.warning(f"  No '{ATTACHMENT_PATTERN}' attachment found — skipping")
                total_skipped += 1
                continue

            filename = os.path.basename(filepath)
            logger.info(f"  Downloaded: {filename}")

            # Parse Excel
            try:
                as_of_date, rows = parse_sales_excel(filepath)
            except Exception as e:
                logger.error(f"  Parse error: {e}")
                total_errors += 1
                continue

            logger.info(f"  As of date: {as_of_date}")
            logger.info(f"  Parsed rows: {len(rows)}")

            # Dedup check
            as_of_str = str(as_of_date)
            if as_of_str in existing_dates:
                logger.info(f"  SKIPPED — as_of_date {as_of_str} already loaded")
                total_skipped += 1
                continue

            # Generate run_id for this file
            run_id = str(uuid.uuid4())
            logger.info(f"  Run ID: {run_id}")

            # Start pipeline run tracking
            write_client = create_supabase_client()
            start_pipeline_run(write_client, run_id, metadata={
                "as_of_date": as_of_str,
                "source_file": filename,
                "email_received_date": received_date.isoformat(),
                "row_count": len(rows),
            })

            try:
                # Load raw records in batches
                email_received_iso = received_date.isoformat()
                for i in range(0, len(rows), LOAD_BATCH_SIZE):
                    batch = rows[i:i + LOAD_BATCH_SIZE]
                    raw_rows = [
                        {
                            "run_id": run_id,
                            "as_of_date": as_of_str,
                            "email_received_date": email_received_iso,
                            "source_file": filename,
                            "data": record,
                        }
                        for record in batch
                    ]
                    load_raw_batch(write_client, raw_rows)

                logger.info(f"  Loaded {len(rows)} raw records")
                total_loaded += len(rows)

                # Transform raw → staging
                transform_client = create_supabase_client()
                transform_sales_for_run(transform_client, run_id)

                complete_pipeline_run(write_client, run_id, "success", records=len(rows))
                existing_dates.add(as_of_str)
                processed_dates.append(as_of_str)

            except Exception as e:
                logger.error(f"  Load/transform error: {e}")
                complete_pipeline_run(write_client, run_id, "failed", error=str(e))
                total_errors += 1

            # Clean up downloaded file
            try:
                os.remove(filepath)
            except OSError:
                pass

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info(f"Sales Detail Pipeline Complete")
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
    parser = argparse.ArgumentParser(description="Extract Sales Detail from Gmail")
    parser.add_argument(
        "--max-emails", type=int, default=100,
        help="Maximum emails to process (default: 100)"
    )
    parser.add_argument(
        "--reprocess", action="store_true",
        help="Re-process even if as_of_date already exists"
    )
    args = parser.parse_args()

    run_sales_pipeline(
        max_emails=args.max_emails,
        reprocess=args.reprocess
    )
