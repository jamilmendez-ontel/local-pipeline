"""
run_gmail_pipelines.py -- Poll Gmail for today's Daily Revenue Report.

Designed to be called hourly by Task Scheduler (1 AM - 10 AM).
Checks if today's as_of_date already exists in both raw_ar_aging and
raw_sales_detail. Skips whichever pipeline already has today's data.
Exits cleanly if both are already loaded.

Usage:
    python run_gmail_pipelines.py
"""

import sys
from datetime import datetime, timezone, timedelta

from config import setup_logging, get_logger, get_db, SCHEMA_RAW

setup_logging()
logger = get_logger("gmail_scheduler")

# Today's date in Eastern Time (reports use business day)
ET_OFFSET = timezone(timedelta(hours=-5))


def get_today_date_str() -> str:
    """Get today's date string in YYYY-MM-DD format (Eastern Time)."""
    return datetime.now(ET_OFFSET).strftime("%Y-%m-%d")


def has_todays_data(db, table: str, today: str) -> bool:
    """Check if today's email received date already exists in a raw table."""
    count = db.fetchval(
        f'SELECT COUNT(*) FROM {SCHEMA_RAW}.{table} WHERE email_received_date::date = $1::date',
        today
    )
    return count is not None and count > 0


def main():
    today = get_today_date_str()
    logger.info(f"Gmail Pipeline Check - {today}")

    db = get_db()

    aging_done = has_todays_data(db, "raw_ar_aging", today)
    sales_done = has_todays_data(db, "raw_sales_detail", today)

    if aging_done:
        logger.info(f"  AR Aging: already loaded for {today}")
    if sales_done:
        logger.info(f"  Sales Detail: already loaded for {today}")

    if aging_done and sales_done:
        logger.info("Both pipelines already have today's data. Nothing to do.")
        return 0

    # Run whichever pipeline still needs today's data
    from main import run_pipeline_with_notification

    if not aging_done:
        logger.info("Running AR Aging pipeline...")
        try:
            from main import run_aging_pipeline_full
            run_pipeline_with_notification(
                run_aging_pipeline_full, "AR Aging", send_email=True
            )
            logger.info("AR Aging pipeline completed.")
        except Exception as e:
            logger.error(f"AR Aging pipeline failed: {e}")

    if not sales_done:
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
