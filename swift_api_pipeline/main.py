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
from config import setup_logging, get_logger, create_supabase_client
from pipeline_notifier import PipelineResult, capture_logs, send_pipeline_email

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
}


def run_orgs_projects_pipeline():
    """Run organizations and projects extraction + transformation"""
    from pipeline import run_orgs_projects_extract
    from transform import run_orgs_projects_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ORGANIZATIONS & PROJECTS PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_orgs_projects_extract()

    client = create_supabase_client()
    run_orgs_projects_transform(run_id, client=client)

    return True


def run_user_priorities_pipeline():
    """Run user priorities extraction + transformation"""
    from pipeline import run_user_priorities_extract
    from transform import run_user_priorities_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# USER PRIORITIES PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_user_priorities_extract()

    client = create_supabase_client()
    run_user_priorities_transform(run_id, client=client)

    return True


def run_asset_tasks_pipeline():
    """Run asset tasks extraction + transformation"""
    from extract_asset_tasks import run_asset_task_pipeline
    from transform import run_assets_transform, run_asset_tasks_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract (returns run_id or raises)
    run_id = run_asset_task_pipeline()

    # Each parallel pipeline gets its own Supabase client for thread safety
    client = create_supabase_client()

    # Transform assets (aggregated from asset tasks)
    run_assets_transform(run_id, client=client)

    # Transform asset tasks (individual task records)
    run_asset_tasks_transform(run_id, client=client)

    return True


def run_forms_pipeline():
    """Run QA forms extraction + transformation"""
    from extract_forms import run_forms_pipeline as extract_forms
    from transform import run_qa_forms_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# QA FORMS PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract (returns run_id or raises)
    run_id = extract_forms()

    # Each parallel pipeline gets its own Supabase client for thread safety
    client = create_supabase_client()

    # Transform
    run_qa_forms_transform(run_id, client=client)

    return True


def run_timer_pipeline_full():
    """Run timer extraction + transformation (append mode)"""
    from extract_timer import run_timer_pipeline
    from transform import run_timer_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# TIMER ACTIVITIES PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract (returns run_id or raises)
    run_id = run_timer_pipeline()

    # Each parallel pipeline gets its own Supabase client for thread safety
    client = create_supabase_client()

    # Transform
    run_timer_transform(run_id, client=client)

    return True


def run_aging_pipeline_full():
    """Run AR aging extraction + transformation (Gmail → Supabase)"""
    from extract_aging import run_aging_pipeline
    from transform import run_ar_aging_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# AR AGING PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract processes all unloaded emails and transforms inline per-file
    # Returns list of processed as_of_dates or None
    result = run_aging_pipeline()

    return True


def run_sales_pipeline_full():
    """Run sales detail extraction + transformation (Gmail → Supabase)"""
    from extract_sales import run_sales_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# SALES BY PRODUCT/SERVICE PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract processes all unloaded emails and transforms inline per-file
    # Returns list of processed as_of_dates or None
    result = run_sales_pipeline()

    return True


def run_pipeline_with_notification(func, name, send_email=True):
    """Run a single pipeline with log capture and email notification."""
    started_at = datetime.now(timezone.utc)
    with capture_logs() as log_handler:
        try:
            func()
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
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
                )
            return True
        except Exception as e:
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
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
                )
            raise


def run_all_pipelines(send_email=True):
    """Run all pipelines — orgs/projects first, then remaining 4 in parallel"""
    overall_start = datetime.now(timezone.utc)
    pipeline_results = []

    with capture_logs() as log_handler:
        logger.info(f"\n{'='*60}")
        logger.info(f"SWIFT API PIPELINE - FULL RUN (PARALLEL)")
        logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}")

        results = {}

        # Phase 1: Orgs/Projects MUST run first (others may depend on reference data)
        p_start = datetime.now(timezone.utc)
        try:
            logger.info(f"\n[{datetime.now():%H:%M:%S}] Starting: Organizations & Projects")
            run_orgs_projects_pipeline()
            p_end = datetime.now(timezone.utc)
            results["Organizations & Projects"] = "SUCCESS"
            pipeline_results.append(PipelineResult(
                pipeline_name="Organizations & Projects", status="SUCCESS",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
            ))
            logger.info(f"Completed: Organizations & Projects")
        except Exception as e:
            p_end = datetime.now(timezone.utc)
            results["Organizations & Projects"] = f"FAILED: {e}"
            pipeline_results.append(PipelineResult(
                pipeline_name="Organizations & Projects", status="FAILED",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
                error_message=str(e),
            ))
            logger.error(f"FAILED: Organizations & Projects - {e}")

        # Phase 2: Remaining pipelines in parallel (no dependencies between them)
        # Stagger starts to avoid overwhelming the Swift API with simultaneous connections
        def staggered_forms():
            time.sleep(10)  # Let asset tasks establish first
            return run_forms_pipeline()

        def staggered_timer():
            time.sleep(5)  # Small delay for timer (lightest pipeline)
            return run_timer_pipeline_full()

        parallel_pipelines = [
            ("Asset Tasks", run_asset_tasks_pipeline),
            ("User Priorities", run_user_priorities_pipeline),
            ("QA Forms", staggered_forms),
            ("Timer Activities", staggered_timer),
        ]

        # Track start times per pipeline
        start_times = {}

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for name, func in parallel_pipelines:
                start_times[name] = datetime.now(timezone.utc)
                futures[executor.submit(func)] = name

            for future in as_completed(futures):
                name = futures[future]
                p_end = datetime.now(timezone.utc)
                p_start = start_times[name]
                try:
                    future.result()
                    results[name] = "SUCCESS"
                    pipeline_results.append(PipelineResult(
                        pipeline_name=name, status="SUCCESS",
                        started_at=p_start, ended_at=p_end,
                        duration_seconds=(p_end - p_start).total_seconds(),
                    ))
                    logger.info(f"Completed: {name}")
                except Exception as e:
                    results[name] = f"FAILED: {e}"
                    pipeline_results.append(PipelineResult(
                        pipeline_name=name, status="FAILED",
                        started_at=p_start, ended_at=p_end,
                        duration_seconds=(p_end - p_start).total_seconds(),
                        error_message=str(e),
                    ))
                    logger.error(f"FAILED: {name} - {e}")

        # Post-Phase 2: Backfill asset_did on timer + QA form from stg_assets
        p_start = datetime.now(timezone.utc)
        try:
            logger.info(f"\n[{datetime.now():%H:%M:%S}] Starting: Asset DID Backfill")
            from transform import backfill_asset_did
            backfill_asset_did()
            p_end = datetime.now(timezone.utc)
            results["Asset DID Backfill"] = "SUCCESS"
            pipeline_results.append(PipelineResult(
                pipeline_name="Asset DID Backfill", status="SUCCESS",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
            ))
            logger.info(f"Completed: Asset DID Backfill")
        except Exception as e:
            p_end = datetime.now(timezone.utc)
            results["Asset DID Backfill"] = f"FAILED: {e}"
            pipeline_results.append(PipelineResult(
                pipeline_name="Asset DID Backfill", status="FAILED",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
                error_message=str(e),
            ))
            logger.error(f"FAILED: Asset DID Backfill - {e}")

        # Post-Phase 2: Refresh analytics materialized views
        p_start = datetime.now(timezone.utc)
        try:
            logger.info(f"\n[{datetime.now():%H:%M:%S}] Starting: Analytics MV Refresh")
            from transform import refresh_analytics
            refresh_analytics()
            p_end = datetime.now(timezone.utc)
            results["Analytics MV Refresh"] = "SUCCESS"
            pipeline_results.append(PipelineResult(
                pipeline_name="Analytics MV Refresh", status="SUCCESS",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
            ))
            logger.info(f"Completed: Analytics MV Refresh")
        except Exception as e:
            p_end = datetime.now(timezone.utc)
            results["Analytics MV Refresh"] = f"FAILED: {e}"
            pipeline_results.append(PipelineResult(
                pipeline_name="Analytics MV Refresh", status="FAILED",
                started_at=p_start, ended_at=p_end,
                duration_seconds=(p_end - p_start).total_seconds(),
                error_message=str(e),
            ))
            logger.error(f"FAILED: Analytics MV Refresh - {e}")

        # Summary
        logger.info(f"\n{'='*60}")
        logger.info(f"PIPELINE SUMMARY")
        logger.info(f"{'='*60}")
        for name, status in results.items():
            logger.info(f"  {name}: {status}")
        logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}\n")

        overall_end = datetime.now(timezone.utc)
        overall_success = all(status == "SUCCESS" for status in results.values())

        if send_email:
            send_pipeline_email(
                results=pipeline_results,
                log_output=log_handler.get_log_output(),
                overall_status="SUCCESS" if overall_success else "FAILED",
                run_label="Full Pipeline Run",
                started_at=overall_start,
                ended_at=overall_end,
                total_duration=(overall_end - overall_start).total_seconds(),
            )

    # Return success if all passed
    return overall_success


def run_all_extractions(send_email=True):
    """Run all extractions only"""
    from pipeline import run_orgs_projects_extract, run_user_priorities_extract
    from extract_asset_tasks import run_asset_task_pipeline
    from extract_forms import run_forms_pipeline as extract_forms
    from extract_timer import run_timer_pipeline

    overall_start = datetime.now(timezone.utc)
    pipeline_results = []

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

        if send_email:
            send_pipeline_email(
                results=pipeline_results,
                log_output=log_handler.get_log_output(),
                overall_status="SUCCESS" if overall_success else "FAILED",
                run_label="Extractions Only",
                started_at=overall_start,
                ended_at=overall_end,
                total_duration=(overall_end - overall_start).total_seconds(),
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

        if send_email:
            send_pipeline_email(
                results=pipeline_results,
                log_output=log_handler.get_log_output(),
                overall_status="SUCCESS" if overall_success else "FAILED",
                run_label="Transformations Only",
                started_at=overall_start,
                ended_at=overall_end,
                total_duration=(overall_end - overall_start).total_seconds(),
            )

    return overall_success


def main():
    parser = argparse.ArgumentParser(
        description="Swift API Pipeline - Extract and transform data from Swift API to Supabase",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                # Run all pipelines (extract + transform)
  python main.py --extract                      # Run all extractions only
  python main.py --transform                    # Run all transformations only
  python main.py --pipeline orgs                # Run orgs/projects pipeline only
  python main.py --pipeline user_priorities     # Run user priorities pipeline only
  python main.py --pipeline asset_tasks         # Run asset_tasks pipeline only
  python main.py --pipeline forms               # Run QA forms pipeline only
  python main.py --pipeline timer               # Run timer pipeline only
  python main.py --pipeline aging               # Run AR aging pipeline only (Gmail)
  python main.py --pipeline sales               # Run sales detail pipeline only (Gmail)
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
        choices=["orgs", "user_priorities", "asset_tasks", "forms", "timer", "aging", "sales"],
        help="Run a specific pipeline (extract + transform)"
    )

    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Suppress email notifications after pipeline run"
    )

    args = parser.parse_args()
    send_email = not args.no_email

    # Map pipeline names to functions
    pipeline_funcs = {
        "orgs": run_orgs_projects_pipeline,
        "user_priorities": run_user_priorities_pipeline,
        "asset_tasks": run_asset_tasks_pipeline,
        "forms": run_forms_pipeline,
        "timer": run_timer_pipeline_full,
        "aging": run_aging_pipeline_full,
        "sales": run_sales_pipeline_full,
    }

    try:
        if args.extract:
            success = run_all_extractions(send_email=send_email)
        elif args.transform:
            success = run_all_transformations(send_email=send_email)
        elif args.pipeline:
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


if __name__ == "__main__":
    sys.exit(main())
