"""
run_gmail_pipelines.py -- Poll Gmail for new Daily Revenue Report emails.

Designed to be called every 15 minutes by Task Scheduler (1 AM - 10 AM).
Checks if Gmail has any unprocessed emails by comparing the most recent
email's received date against the latest loaded email_received_date in
each raw table. Only runs pipelines when new data is detected.

Usage:
    python run_gmail_pipelines.py
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config import setup_logging, get_logger, get_db, SCHEMA_RAW, SCHEMA_PIPELINE

setup_logging()
logger = get_logger("gmail_scheduler")

ET_TZ = ZoneInfo("America/New_York")

# Both pipelines search for the same email subject
GMAIL_QUERY = 'subject:"Daily Revenue Report" has:attachment'


def _get_last_seen_email_ts(db, pipeline_name: str):
    """Get the max email timestamp we've already processed (loaded or skipped).

    Checks pipeline_runs metadata for the most recent email_received_date
    we attempted to process, regardless of whether rows were actually loaded.
    This prevents re-triggering on emails whose attachments were skipped
    (e.g. monthly summaries).
    """
    row = db.fetchval(
        f"SELECT MAX((metadata->>'email_received_date')::timestamptz) "
        f"FROM {SCHEMA_PIPELINE}.pipeline_runs "
        f"WHERE pipeline_name = $1 AND metadata->>'email_received_date' IS NOT NULL",
        pipeline_name
    )
    return row


def has_new_emails(db, service, table: str, pipeline_name: str = None) -> bool:
    """Check if Gmail has emails newer than the latest we've processed.

    Compares against both the max loaded email_received_date in the raw table
    AND the max email timestamp in pipeline_runs (to avoid re-triggering on
    emails that were processed but had no loadable attachment).
    """
    max_loaded = db.fetchval(
        f'SELECT MAX(email_received_date) FROM {SCHEMA_RAW}.{table}'
    )

    # Also check pipeline_runs for emails we've already attempted
    max_seen = _get_last_seen_email_ts(db, pipeline_name) if pipeline_name else None

    # Use the more recent of the two as our watermark
    if max_loaded and max_seen:
        max_date = max(max_loaded, max_seen)
    else:
        max_date = max_loaded or max_seen

    if max_date is None:
        logger.info(f"  {table}: no data loaded yet -- needs full run")
        return True

    from gmail_client import search_messages, get_message_details

    messages = search_messages(service, GMAIL_QUERY, max_results=3)
    if not messages:
        return False

    for msg in messages:
        details = get_message_details(service, msg['id'])
        received = details.get('received_date')
        if received and received > max_date:
            logger.info(
                f"  {table}: new email found (received {received:%Y-%m-%d %H:%M:%S} "
                f"> latest processed {max_date:%Y-%m-%d %H:%M:%S})"
            )
            return True

    return False


def main():
    now_et = datetime.now(ET_TZ)
    logger.info(f"Gmail Pipeline Check - {now_et:%Y-%m-%d %H:%M:%S %Z}")

    db = get_db()

    # Authenticate to Gmail once for both checks
    from gmail_client import authenticate
    service = authenticate()

    aging_new = has_new_emails(db, service, "raw_ar_aging", pipeline_name="ar_aging_extract")
    sales_new = has_new_emails(db, service, "raw_sales_detail", pipeline_name="sales_detail_extract")

    if not aging_new:
        logger.info("  AR Aging: no new emails")
    if not sales_new:
        logger.info("  Sales Detail: no new emails")

    if not aging_new and not sales_new:
        logger.info("No new emails detected. Nothing to do.")
        return 0

    # Run whichever pipeline has new data
    from main import run_pipeline_with_notification

    # Gmail pipelines only notify Jamil (not the full team)
    gmail_recipients = ["jamil.mendez@ontel.co"]

    if aging_new:
        logger.info("Running AR Aging pipeline...")
        try:
            from main import run_aging_pipeline_full
            run_pipeline_with_notification(
                run_aging_pipeline_full, "AR Aging", send_email=True,
                recipients=gmail_recipients
            )
            logger.info("AR Aging pipeline completed.")
        except Exception as e:
            logger.error(f"AR Aging pipeline failed: {e}")

    if sales_new:
        logger.info("Running Sales Detail pipeline...")
        try:
            from main import run_sales_pipeline_full
            run_pipeline_with_notification(
                run_sales_pipeline_full, "Sales Detail", send_email=True,
                recipients=gmail_recipients
            )
            logger.info("Sales Detail pipeline completed.")
        except Exception as e:
            logger.error(f"Sales Detail pipeline failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
