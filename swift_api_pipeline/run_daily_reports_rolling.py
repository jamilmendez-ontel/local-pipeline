"""Run the Daily Reports ROLLING pipeline.

One job that replaces the split daily + requirements jobs: refreshes a rolling
last-N-days window (default 30) of ALL THREE datasets every run —
  - task status/approval -> data_staging.stg_daily_reports
  - requirements (hours)  -> data_staging.stg_daily_report_hours
  - timers (attendance)   -> data_staging.stg_daily_report_attendance
No success email; failure email only. Runtime is recorded to
pipeline.pipeline_runs as 'daily_reports_rolling'.

Usage:
    python run_daily_reports_rolling.py            # last 30 days
    python run_daily_reports_rolling.py --days 35
"""

import argparse
import sys

from base_extractor import BaseExtractor
from config import get_db, get_logger, setup_logging
from main import run_pipeline_with_notification

# Unbuffered output (match the other run_*.py scripts)
sys.stdout.reconfigure(line_buffering=True)

setup_logging()
logger = get_logger("run_daily_reports_rolling")


def run_rolling(days=30):
    """Refresh the rolling window: tasks + requirements + timers for last `days`.

    Calling DailyReportsPipeline.run() with neither timers_only nor
    requirements_only runs Step 4a (requirements) AND Step 4b (timers) plus the
    always-on task load — i.e. all three datasets in one pass.
    """
    from extract_daily_reports import DailyReportsPipeline, discover_projects

    tracker = BaseExtractor(pipeline_name="daily_reports_rolling")
    tracker.start_pipeline_run(metadata={"days": days, "window": "rolling"})
    try:
        logger.info(f"=== ROLLING MODE: all datasets, last {days} days ===")
        projects = discover_projects()
        if not projects:
            logger.info("No active Daily Reports projects found.")
            # discover_projects() called close_db() (stops the shared event loop and
            # nulls the singleton). Re-point the tracker at a fresh live pool before
            # writing the completion row — same run_id, so it updates the 'running' row.
            tracker.db = get_db()
            tracker.complete_pipeline_run("success", records=0)
            return
        pipeline = DailyReportsPipeline()
        counts = pipeline.run(projects, full=False, days=days)
        records = sum((counts or {}).values())
        # discover_projects()/run() each call close_db(), which stops the shared event
        # loop AND nulls the db singleton. The tracker's cached self.db is now stale, so
        # re-point it at a fresh live pool (same run_id) before completing the run row.
        tracker.db = get_db()
        tracker.complete_pipeline_run("success", records=records)
    except Exception as e:
        # Same reason: ensure a live pool before recording the failure.
        try:
            tracker.db = get_db()
        except Exception:
            pass
        tracker.complete_pipeline_run("failed", error=str(e))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Rolling look-back window (default 30)")
    parser.add_argument("--no-email", action="store_true", help="Suppress the failure email too")
    args = parser.parse_args()

    # email_on_success=False -> no email on success; failure email still fires
    # (and re-raises -> non-zero exit -> GHA marks the run failed).
    run_pipeline_with_notification(
        lambda: run_rolling(days=args.days),
        "Daily Reports",
        send_email=not args.no_email,
        logger_prefixes=[
            "pipeline.daily_reports",
            "pipeline.run_daily_reports_rolling",
            "pipeline.base",
            "pipeline.db",
        ],
        recipients=["jamil.mendez@ontel.co"],
        email_on_success=False,
    )


if __name__ == "__main__":
    main()
