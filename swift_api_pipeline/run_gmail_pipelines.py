"""
run_gmail_pipelines.py -- Poll Gmail for new Daily Revenue Report emails.

Designed to be called every 30 minutes by Task Scheduler (1 AM - 10 AM).
Checks if Gmail has any unprocessed emails by comparing the most recent
email's received date against the latest loaded email_received_date in
each raw table. Only runs pipelines when new data is detected.

Usage:
    python run_gmail_pipelines.py
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config import setup_logging, get_logger, get_db, SCHEMA_RAW

setup_logging()
logger = get_logger("gmail_scheduler")

ET_TZ = ZoneInfo("America/New_York")

# Both pipelines search for the same email subject
GMAIL_QUERY = 'subject:"Daily Revenue Report" has:attachment'


def has_new_emails(db, service, table: str) -> bool:
    """Check if Gmail has emails newer than the latest loaded in a raw table.

    Compares the 3 most recent matching Gmail messages against our max
    email_received_date. Only makes 1 search call + up to 3 detail calls.
    """
    max_date = db.fetchval(
        f'SELECT MAX(email_received_date) FROM {SCHEMA_RAW}.{table}'
    )

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
                f"> latest loaded {max_date:%Y-%m-%d %H:%M:%S})"
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

    aging_new = has_new_emails(db, service, "raw_ar_aging")
    sales_new = has_new_emails(db, service, "raw_sales_detail")

    if not aging_new:
        logger.info("  AR Aging: no new emails")
    if not sales_new:
        logger.info("  Sales Detail: no new emails")

    if not aging_new and not sales_new:
        logger.info("No new emails detected. Nothing to do.")
        return 0

    # Run whichever pipeline has new data
    from main import run_pipeline_with_notification

    if aging_new:
        logger.info("Running AR Aging pipeline...")
        try:
            from main import run_aging_pipeline_full
            run_pipeline_with_notification(
                run_aging_pipeline_full, "AR Aging", send_email=True
            )
            logger.info("AR Aging pipeline completed.")
        except Exception as e:
            logger.error(f"AR Aging pipeline failed: {e}")

    if sales_new:
        logger.info("Running Sales Detail pipeline...")
        try:
            from main import run_sales_pipeline_full
            run_pipeline_with_notification(
                run_sales_pipeline_full, "Sales Detail", send_email=True
            )
            logger.info("Sales Detail pipeline completed.")
        except Exception as e:
            logger.error(f"Sales Detail pipeline failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
