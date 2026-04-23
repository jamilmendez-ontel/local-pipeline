#!/usr/bin/env python3
"""
Swift API Pipeline - Main Entry Point
Runs all extraction and transformation pipelines in sequence

Usage:
    python main.py              # Run all pipelines
    python main.py --extract    # Run extractions only
    python main.py --transform  # Run transformations only
    python main.py --pipeline asset_tasks  # Run specific pipeline
    python main.py --no-email   # Suppress email notifications
"""

import sys
import time
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import setup_logging, get_logger
from db import close_db
from pipeline_notifier import PipelineResult, PIPELINE_TABLES, ALL_TABLES, capture_logs, send_pipeline_email, snapshot_row_counts

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# Initialize logging for all pipeline modules
setup_logging()
logger = get_logger("main")


# Pipeline name mapping for email subjects
PIPELINE_NAMES = {
    "orgs": "Organizations & Projects",
    "user_priorities": "User Priorities",
    "asset_tasks": "Asset Tasks",
    "forms": "QA Forms",
    "timer": "Timer Activities",
    "aging": "AR Aging",
    "sales": "Sales Detail",
    "backfill": "Asset DID Backfill",
    "analytics": "Analytics MV Refresh",
    "assets": "Assets Status",
}


def run_orgs_projects_pipeline():
    """Run organizations and projects extraction + transformation"""
    from pipeline import run_orgs_projects_extract
    from transform import run_orgs_projects_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ORGANIZATIONS & PROJECTS PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_orgs_projects_extract()
    run_orgs_projects_transform(run_id)

    return True


def run_user_priorities_pipeline():
    """Run user priorities extraction + transformation"""
    from pipeline import run_user_priorities_extract
    from transform import run_user_priorities_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# USER PRIORITIES PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_user_priorities_extract()
    run_user_priorities_transform(run_id)

    return True


def run_asset_tasks_pipeline(project_filter: str = None):
    """Run asset tasks extraction + transformation.

    project_filter: if set, runs in single-project recovery mode (e.g. 'TS16').
        Reuses the latest run_id, cleans only that project's raw rows, re-extracts,
        then runs transforms + backfill + analytics refresh.
    """
    from extract_asset_tasks import run_asset_task_pipeline
    from transform import run_assets_transform, run_asset_tasks_transform

    logger.info(f"\n{'#'*60}")
    if project_filter:
        logger.info(f"# ASSET TASKS PIPELINE (RECOVERY: {project_filter})")
    else:
        logger.info(f"# ASSET TASKS PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_asset_task_pipeline(project_filter=project_filter)

    # Transform assets (aggregated from asset tasks)
    run_assets_transform(run_id)

    # Transform asset tasks (individual task records)
    run_asset_tasks_transform(run_id)

    # After single-project recovery, also update backfill and analytics
    if project_filter:
        from transform import backfill_asset_did, refresh_analytics
        logger.info("Recovery: running backfill_asset_did and analytics refresh...")
        backfill_asset_did()
        refresh_analytics()

    return True


def run_forms_pipeline():
    """Run QA forms extraction + transformation"""
    from extract_forms import run_forms_pipeline as extract_forms
    from transform import run_qa_forms_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# QA FORMS PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = extract_forms()
    run_qa_forms_transform(run_id)

    return True


def run_timer_pipeline_full():
    """Run timer extraction + transformation (append mode)"""
    from extract_timer import run_timer_pipeline
    from transform import run_timer_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# TIMER ACTIVITIES PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_timer_pipeline()
    run_timer_transform(run_id)

    return True


def run_backfill():
    """Run asset DID backfill on timer + QA form tables"""
    from transform import backfill_asset_did

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET DID BACKFILL")
    logger.info(f"{'#'*60}")

    backfill_asset_did()
    return True


def run_assets_pipeline():
    """Extract assets from Swift + enrich stg_assets.asset_status.

    Runs AFTER Phase 2 so that stg_assets exists (built by asset_tasks transform).
    ~30-60s end-to-end for the 7 TECH-OPS TS13+ projects.
    """
    from extract_assets import run_assets_extract
    from transform import enrich_stg_assets_with_status

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSETS EXTRACT + STATUS ENRICHMENT")
    logger.info(f"{'#'*60}")

    run_assets_extract()
    enrich_stg_assets_with_status()
    return True


def run_analytics_refresh():
    """Refresh analytics materialized views"""
    from transform import refresh_analytics

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ANALYTICS MV REFRESH")
    logger.info(f"{'#'*60}")

    refresh_analytics()
    return True


def run_aging_pipeline_full():
    """Run AR aging extraction + transformation (Gmail)"""
    from extract_aging import run_aging_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# AR AGING PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract processes all unloaded emails and transforms inline per-file
    result = run_aging_pipeline()

    return True


def run_sales_pipeline_full():
    """Run sales detail extraction + transformation (Gmail)"""
    from extract_sales import run_sales_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# SALES BY PRODUCT/SERVICE PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract processes all unloaded emails and transforms inline per-file
    result = run_sales_pipeline()

    return True


def run_pipeline_with_notification(func, name, send_email=True, logger_prefixes=None, recipients=None):
    """Run a single pipeline with log capture and email notification."""
    tables = PIPELINE_TABLES.get(name)
    started_at = datetime.now(timezone.utc)
    row_counts_before = snapshot_row_counts(tables)
    with capture_logs(logger_prefixes=logger_prefixes) as log_handler:
        try:
            func()
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)
            result = PipelineResult(
                pipeline_name=name,
                status="SUCCESS",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration,
            )
            if send_email:
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="SUCCESS",
                    run_label=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    recipients=recipients,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )
            return True
        except Exception as e:
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)
            result = PipelineResult(
                pipeline_name=name,
                status="FAILED",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration,
                error_message=str(e),
            )
            if send_email:
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="FAILED",
                    run_label=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    recipients=recipients,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )
            raise


def _run_and_notify(func, name, send_email=True, logger_prefixes=None):
    """Run a single pipeline step with its own log capture, row counts, and email.

    Returns a PipelineResult. Never raises."""
    started_at = datetime.now(timezone.utc)
    try:
        run_pipeline_with_notification(func, name, send_email, logger_prefixes=logger_prefixes)
        ended_at = datetime.now(timezone.utc)
        return PipelineResult(
            pipeline_name=name,
            status="SUCCESS",
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=(ended_at - started_at).total_seconds(),
        )
    except Exception as e:
        # Individual email already sent by run_pipeline_with_notification
        ended_at = datetime.now(timezone.utc)
        return PipelineResult(
            pipeline_name=name,
            status="FAILED",
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=(ended_at - started_at).total_seconds(),
            error_message=str(e),
        )


def run_all_pipelines(send_email=True):
    """Run all pipelines with individual email notifications per pipeline.

    Phase 1: Orgs/Projects (sequential, must run first).
    Phase 2: Asset Tasks, User Priorities, QA Forms, Timer (parallel).
    Post-Phase 2: Asset DID Backfill, Analytics MV Refresh (sequential).
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"SWIFT API PIPELINE - FULL RUN (PARALLEL)")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    pipeline_results = []

    # Phase 1: Orgs/Projects MUST run first (others may depend on reference data)
    result = _run_and_notify(run_orgs_projects_pipeline, "Organizations & Projects", send_email)
    pipeline_results.append(result)

    # Phase 2: Remaining pipelines in parallel (no dependencies between them)
    # Stagger starts to avoid overwhelming the Swift API with simultaneous connections
    def staggered_forms():
        time.sleep(10)  # Let asset tasks establish first
        return run_forms_pipeline()

    def staggered_timer():
        time.sleep(5)  # Small delay for timer (lightest pipeline)
        return run_timer_pipeline_full()

    # Logger name prefixes for each parallel pipeline — used to filter
    # cross-contamination in email log attachments.  Thread-ID filtering
    # catches shared loggers (base, retry, db, transform) from the main
    # thread; these prefixes catch child worker threads (e.g. asset_tasks'
    # 6 extraction workers logging to pipeline.asset_tasks).
    parallel_pipelines = [
        ("Asset Tasks", run_asset_tasks_pipeline, ["pipeline.asset_tasks"]),
        ("User Priorities", run_user_priorities_pipeline, ["pipeline.user_priorities"]),
        ("QA Forms", staggered_forms, ["pipeline.forms"]),
        ("Timer Activities", staggered_timer, ["pipeline.timer"]),
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                _run_and_notify, func, name, send_email,
                logger_prefixes=prefixes,
            ): name
            for name, func, prefixes in parallel_pipelines
        }

        for future in as_completed(futures):
            result = future.result()
            pipeline_results.append(result)

    # Post-Phase 2: Assets extract + status enrichment (must run AFTER asset_tasks
    # transform has populated stg_assets -- we UPDATE asset_status on existing rows).
    result = _run_and_notify(run_assets_pipeline, "Assets Status", send_email=False)
    pipeline_results.append(result)

    # Post-Phase 2: Backfill asset_did on timer + QA form from stg_assets
    from transform import backfill_asset_did, refresh_analytics

    result = _run_and_notify(backfill_asset_did, "Asset DID Backfill", send_email=False)
    pipeline_results.append(result)

    # Post-Phase 2: Refresh analytics materialized views
    result = _run_and_notify(refresh_analytics, "Analytics MV Refresh", send_email=False)
    pipeline_results.append(result)

    # Summary log
    logger.info(f"\n{'='*60}")
    logger.info(f"PIPELINE SUMMARY")
    logger.info(f"{'='*60}")
    for r in pipeline_results:
        err = f" ({r.error_message})" if r.error_message else ""
        logger.info(f"  {r.pipeline_name}: {r.status}{err}")
    logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    overall_success = all(r.status == "SUCCESS" for r in pipeline_results)

    # Downstream: notify cop-date-validator that fresh asset_tasks data is ready.
    # Never fail the pipeline on a dispatch error.
    if overall_success:
        try:
            from github_trigger import fire_dispatch
            fire_dispatch(
                "jamilmendez-ontel/cop-date-validator",
                "cop-date-validator-daily",
                client_payload={"source": "asset_tasks"},
            )
        except Exception as e:
            logger.warning(f"downstream dispatch failed: {type(e).__name__}: {e}")

    return overall_success


def run_all_extractions(send_email=True):
    """Run all extractions only"""
    from pipeline import run_orgs_projects_extract, run_user_priorities_extract
    from extract_asset_tasks import run_asset_task_pipeline
    from extract_forms import run_forms_pipeline as extract_forms
    from extract_timer import run_timer_pipeline

    overall_start = datetime.now(timezone.utc)
    pipeline_results = []
    row_counts_before = snapshot_row_counts(ALL_TABLES)

    with capture_logs() as log_handler:
        logger.info(f"\n{'='*60}")
        logger.info(f"SWIFT API PIPELINE - EXTRACTIONS ONLY")
        logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}")

        results = {}

        extraction_steps = [
            ("Organizations & Projects", run_orgs_projects_extract),
            ("User Priorities", run_user_priorities_extract),
            ("Asset Tasks", run_asset_task_pipeline),
            ("QA Forms", extract_forms),
            ("Timer Activities", run_timer_pipeline),
        ]

        for name, func in extraction_steps:
            p_start = datetime.now(timezone.utc)
            try:
                logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting {name}...")
                func()
                p_end = datetime.now(timezone.utc)
                results[name] = "SUCCESS"
                pipeline_results.append(PipelineResult(
                    pipeline_name=name, status="SUCCESS",
                    started_at=p_start, ended_at=p_end,
                    duration_seconds=(p_end - p_start).total_seconds(),
                ))
            except Exception as e:
                p_end = datetime.now(timezone.utc)
                results[name] = f"FAILED: {e}"
                pipeline_results.append(PipelineResult(
                    pipeline_name=name, status="FAILED",
                    started_at=p_start, ended_at=p_end,
                    duration_seconds=(p_end - p_start).total_seconds(),
                    error_message=str(e),
                ))

        # Summary
        logger.info(f"\n{'='*60}")
        logger.info(f"EXTRACTION SUMMARY")
        logger.info(f"{'='*60}")
        for name, status in results.items():
            logger.info(f"  {name}: {status}")
        logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}\n")

        overall_end = datetime.now(timezone.utc)
        overall_success = all(status == "SUCCESS" for status in results.values())

        row_counts_after = snapshot_row_counts(ALL_TABLES)

        if send_email:
            send_pipeline_email(
                results=pipeline_results,
                log_output=log_handler.get_log_output(),
                overall_status="SUCCESS" if overall_success else "FAILED",
                run_label="Extractions Only",
                started_at=overall_start,
                ended_at=overall_end,
                total_duration=(overall_end - overall_start).total_seconds(),
                row_counts_before=row_counts_before,
                row_counts_after=row_counts_after,
                row_count_tables=ALL_TABLES,
            )

    return overall_success


def run_all_transformations(send_email=True):
    """Run all transformations only (uses latest successful extractions)"""
    from transform import (
        run_orgs_projects_transform, run_user_priorities_transform,
        run_assets_transform, run_asset_tasks_transform,
        run_qa_forms_transform, run_timer_transform,
        backfill_asset_did, refresh_analytics
    )

    overall_start = datetime.now(timezone.utc)
    pipeline_results = []
    row_counts_before = snapshot_row_counts(ALL_TABLES)

    with capture_logs() as log_handler:
        logger.info(f"\n{'='*60}")
        logger.info(f"SWIFT API PIPELINE - TRANSFORMATIONS ONLY")
        logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}")

        results = {}

        transform_steps = [
            ("Organizations & Projects", run_orgs_projects_transform),
            ("User Priorities", run_user_priorities_transform),
            ("Assets", run_assets_transform),
            ("Asset Tasks", run_asset_tasks_transform),
            ("QA Forms", run_qa_forms_transform),
            ("Timer Activities", run_timer_transform),
            ("Asset DID Backfill", backfill_asset_did),
            ("Analytics MV Refresh", refresh_analytics),
        ]

        for name, func in transform_steps:
            p_start = datetime.now(timezone.utc)
            try:
                logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming {name}...")
                func()
                p_end = datetime.now(timezone.utc)
                results[name] = "SUCCESS"
                pipeline_results.append(PipelineResult(
                    pipeline_name=name, status="SUCCESS",
                    started_at=p_start, ended_at=p_end,
                    duration_seconds=(p_end - p_start).total_seconds(),
                ))
            except Exception as e:
                p_end = datetime.now(timezone.utc)
                results[name] = f"FAILED: {e}"
                pipeline_results.append(PipelineResult(
                    pipeline_name=name, status="FAILED",
                    started_at=p_start, ended_at=p_end,
                    duration_seconds=(p_end - p_start).total_seconds(),
                    error_message=str(e),
                ))

        # Summary
        logger.info(f"\n{'='*60}")
        logger.info(f"TRANSFORMATION SUMMARY")
        logger.info(f"{'='*60}")
        for name, status in results.items():
            logger.info(f"  {name}: {status}")
        logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}\n")

        overall_end = datetime.now(timezone.utc)
        overall_success = all(status == "SUCCESS" for status in results.values())

        row_counts_after = snapshot_row_counts(ALL_TABLES)

        if send_email:
            send_pipeline_email(
                results=pipeline_results,
                log_output=log_handler.get_log_output(),
                overall_status="SUCCESS" if overall_success else "FAILED",
                run_label="Transformations Only",
                started_at=overall_start,
                ended_at=overall_end,
                total_duration=(overall_end - overall_start).total_seconds(),
                row_counts_before=row_counts_before,
                row_counts_after=row_counts_after,
                row_count_tables=ALL_TABLES,
            )

    return overall_success


def main():
    parser = argparse.ArgumentParser(
        description="Swift API Pipeline - Extract and transform data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                # Run all pipelines (extract + transform)
  python main.py --extract                      # Run all extractions only
  python main.py --transform                    # Run all transformations only
  python main.py --pipeline orgs                # Run orgs/projects pipeline only
  python main.py --pipeline user_priorities     # Run user priorities pipeline only
  python main.py --pipeline asset_tasks         # Run asset_tasks pipeline only
  python main.py --pipeline asset_tasks --project TS16  # Recover single project
  python main.py --pipeline forms               # Run QA forms pipeline only
  python main.py --pipeline timer               # Run timer pipeline only
  python main.py --pipeline aging               # Run AR aging pipeline only (Gmail)
  python main.py --pipeline sales               # Run sales detail pipeline only (Gmail)
  python main.py --pipeline backfill            # Run asset DID backfill only
  python main.py --pipeline analytics           # Run analytics MV refresh only
  python main.py --no-email                     # Run all pipelines without email notification
        """
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--extract",
        action="store_true",
        help="Run extractions only (no transformations)"
    )
    group.add_argument(
        "--transform",
        action="store_true",
        help="Run transformations only (uses latest extractions)"
    )
    group.add_argument(
        "--pipeline",
        type=str,
        choices=["orgs", "user_priorities", "asset_tasks", "forms", "timer", "aging", "sales", "backfill", "analytics", "assets"],
        help="Run a specific pipeline (extract + transform)"
    )

    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Suppress email notifications after pipeline run"
    )
    parser.add_argument(
        "--project",
        type=str,
        metavar="TS16",
        help="Recover a single project (use with --pipeline asset_tasks only). E.g. --project TS16"
    )

    args = parser.parse_args()
    send_email = not args.no_email

    if args.project and args.pipeline != "asset_tasks":
        parser.error("--project can only be used with --pipeline asset_tasks")

    # Map pipeline names to functions
    pipeline_funcs = {
        "orgs": run_orgs_projects_pipeline,
        "user_priorities": run_user_priorities_pipeline,
        "asset_tasks": run_asset_tasks_pipeline,
        "forms": run_forms_pipeline,
        "timer": run_timer_pipeline_full,
        "aging": run_aging_pipeline_full,
        "sales": run_sales_pipeline_full,
        "backfill": run_backfill,
        "analytics": run_analytics_refresh,
        "assets": run_assets_pipeline,
    }

    try:
        if args.extract:
            success = run_all_extractions(send_email=send_email)
        elif args.transform:
            success = run_all_transformations(send_email=send_email)
        elif args.pipeline:
            if args.pipeline == "asset_tasks" and args.project:
                func = lambda: run_asset_tasks_pipeline(project_filter=args.project)
            else:
                func = pipeline_funcs[args.pipeline]
            name = PIPELINE_NAMES[args.pipeline]
            success = run_pipeline_with_notification(func, name, send_email=send_email)
        else:
            # Default: run all
            success = run_all_pipelines(send_email=send_email)

        return 0 if success else 1

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        return 130
    except Exception as e:
        logger.info(f"\n\nPipeline failed with error: {e}")
        return 1
    finally:
        close_db()


if __name__ == "__main__":
    sys.exit(main())
