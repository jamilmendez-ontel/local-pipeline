"""Run Daily Reports pipeline with different modes.

Modes:
    --daily          Timers only for last N days (default 3)
    --requirements   Requirements for the current bi-monthly period
                     Checks if period is fully approved → sends email notification → stops
    --full           Full extract (all data)

Usage:
    python run_daily_reports.py --daily --days 3
    python run_daily_reports.py --requirements
    python run_daily_reports.py --full
"""

import argparse
import sys
from datetime import date, timedelta

from config import SCHEMA_STAGING, get_logger, get_db, close_db, retry_db, setup_logging

setup_logging()
logger = get_logger("run_daily_reports")


def get_current_period():
    """Get the bi-monthly period that should be checked for requirements.

    Run windows:
      2nd-5th: check previous month's 16th-end
      17th-20th: check current month's 1st-15th
    """
    today = date.today()
    day = today.day

    if 2 <= day <= 5:
        # Check previous period: 16th to end of last month
        first_of_month = today.replace(day=1)
        period_end = first_of_month - timedelta(days=1)  # last day of prev month
        period_start = period_end.replace(day=16)
        return period_start, period_end
    elif 17 <= day <= 20:
        # Check current period: 1st to 15th
        period_start = today.replace(day=1)
        period_end = today.replace(day=15)
        return period_start, period_end
    else:
        # Outside run window — return None
        return None, None


def check_period_complete(period_start, period_end):
    """Check if all tasks in the period are approved or cancelled.

    Returns (is_complete, total, approved, cancelled, remaining).
    """
    db = get_db()
    result = retry_db(
        lambda: db.fetchrow(
            f"SELECT "
            f"  COUNT(*) AS total, "
            f"  COUNT(*) FILTER (WHERE task_status = 'approved') AS approved, "
            f"  COUNT(*) FILTER (WHERE task_status = 'cancelled') AS cancelled, "
            f"  COUNT(*) FILTER (WHERE task_status NOT IN ('approved', 'cancelled')) AS remaining "
            f"FROM {SCHEMA_STAGING}.stg_daily_report_tasks "
            f"WHERE work_date >= $1 AND work_date <= $2",
            period_start, period_end,
        ),
        description="check period status",
    )
    close_db()

    total = result["total"]
    approved = result["approved"]
    cancelled = result["cancelled"]
    remaining = result["remaining"]
    is_complete = remaining == 0 and total > 0

    return is_complete, total, approved, cancelled, remaining


def send_period_complete_email(period_start, period_end, total, approved, cancelled):
    """Send notification that all daily reports for the period are approved."""
    try:
        from gmail_client import authenticate
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        import base64

        service = authenticate()
        period_str = f"{period_start.strftime('%b %d')} - {period_end.strftime('%b %d, %Y')}"

        html = f"""
        <html>
        <body style="font-family:Arial,sans-serif;">
            <div style="background:#2e7d32;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Daily Reports — Period Complete</h2>
            </div>
            <div style="padding:24px;">
                <p>All daily reports for <strong>{period_str}</strong> have been reviewed.</p>
                <table style="border-collapse:collapse;margin:16px 0;">
                    <tr><td style="padding:8px 16px;border:1px solid #ddd;"><strong>Total Tasks</strong></td>
                        <td style="padding:8px 16px;border:1px solid #ddd;">{total}</td></tr>
                    <tr><td style="padding:8px 16px;border:1px solid #ddd;"><strong>Approved</strong></td>
                        <td style="padding:8px 16px;border:1px solid #ddd;color:#2e7d32;">{approved}</td></tr>
                    <tr><td style="padding:8px 16px;border:1px solid #ddd;"><strong>Cancelled</strong></td>
                        <td style="padding:8px 16px;border:1px solid #ddd;color:#888;">{cancelled}</td></tr>
                </table>
                <p style="color:#888;font-size:12px;">
                    Requirements sync for this period will stop. Next period sync will start automatically.
                </p>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg["To"] = "jamil.mendez@ontel.co"
        msg["From"] = "me"
        msg["Subject"] = f"Daily Reports Complete — {period_str}"
        msg.attach(MIMEText(html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Sent period complete notification for {period_str}")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


def run_daily(days=3):
    """Daily mode: fetch timers only for the last N days."""
    from extract_daily_reports import DailyReportsPipeline, discover_projects

    logger.info(f"=== DAILY MODE: Timers for last {days} days ===")
    projects = discover_projects()
    if not projects:
        return

    pipeline = DailyReportsPipeline()
    pipeline.run(projects, full=False, days=days, timers_only=True)


def run_requirements():
    """Bi-monthly mode: fetch requirements for the current period, check completion."""
    period_start, period_end = get_current_period()

    if not period_start:
        logger.info("Outside requirements run window (2nd-5th, 17th-20th). Skipping.")
        return

    logger.info(f"=== REQUIREMENTS MODE: {period_start} to {period_end} ===")

    # Check if already complete
    is_complete, total, approved, cancelled, remaining = check_period_complete(period_start, period_end)
    if is_complete:
        logger.info(f"Period already complete ({approved} approved, {cancelled} cancelled). Skipping.")
        return

    logger.info(f"Period status: {approved} approved, {cancelled} cancelled, {remaining} remaining")

    # Run extraction for this period
    from extract_daily_reports import DailyReportsPipeline, discover_projects

    projects = discover_projects()
    if not projects:
        return

    pipeline = DailyReportsPipeline()
    # Calculate days from period_start to today
    days_back = (date.today() - period_start).days + 1
    pipeline.run(projects, full=False, days=days_back, requirements_only=True)

    # Re-check after extraction
    is_complete, total, approved, cancelled, remaining = check_period_complete(period_start, period_end)
    if is_complete:
        logger.info(f"Period COMPLETE: {approved} approved, {cancelled} cancelled")
        send_period_complete_email(period_start, period_end, total, approved, cancelled)
    else:
        logger.info(f"Period not yet complete: {remaining} tasks remaining")


def run_full():
    """Full mode: extract everything."""
    from extract_daily_reports import DailyReportsPipeline, discover_projects

    logger.info("=== FULL MODE ===")
    projects = discover_projects()
    if not projects:
        return

    pipeline = DailyReportsPipeline()
    pipeline.run(projects, full=True)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--daily", action="store_true", help="Timers only for last N days")
    group.add_argument("--requirements", action="store_true", help="Requirements for current bi-monthly period")
    group.add_argument("--full", action="store_true", help="Full extract")
    parser.add_argument("--days", type=int, default=3, help="Days to look back (daily mode)")
    args = parser.parse_args()

    if args.daily:
        run_daily(days=args.days)
    elif args.requirements:
        run_requirements()
    elif args.full:
        run_full()


if __name__ == "__main__":
    main()
