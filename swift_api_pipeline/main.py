#!/usr/bin/env python3
"""
Swift API Pipeline - Main Entry Point
Runs all extraction and transformation pipelines in sequence

Usage:
    python main.py              # Run all pipelines
    python main.py --extract    # Run extractions only
    python main.py --transform  # Run transformations only
    python main.py --pipeline asset_tasks  # Run specific pipeline
"""

import sys
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import setup_logging, get_logger, create_supabase_client

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# Initialize logging for all pipeline modules
setup_logging()
logger = get_logger("main")


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


def run_all_pipelines():
    """Run all pipelines — orgs/projects first, then remaining 3 in parallel"""
    logger.info(f"\n{'='*60}")
    logger.info(f"SWIFT API PIPELINE - FULL RUN (PARALLEL)")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    results = {}

    # Phase 1: Orgs/Projects MUST run first (others may depend on reference data)
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Starting: Organizations & Projects")
        run_orgs_projects_pipeline()
        results["Organizations & Projects"] = "SUCCESS"
        logger.info(f"Completed: Organizations & Projects")
    except Exception as e:
        results["Organizations & Projects"] = f"FAILED: {e}"
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

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(func): name
            for name, func in parallel_pipelines
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
                results[name] = "SUCCESS"
                logger.info(f"Completed: {name}")
            except Exception as e:
                results[name] = f"FAILED: {e}"
                logger.error(f"FAILED: {name} - {e}")

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info(f"PIPELINE SUMMARY")
    logger.info(f"{'='*60}")
    for name, status in results.items():
        logger.info(f"  {name}: {status}")
    logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    # Return success if all passed
    return all(status == "SUCCESS" for status in results.values())


def run_all_extractions():
    """Run all extractions only"""
    from pipeline import run_orgs_projects_extract, run_user_priorities_extract
    from extract_asset_tasks import run_asset_task_pipeline
    from extract_forms import run_forms_pipeline as extract_forms
    from extract_timer import run_timer_pipeline

    logger.info(f"\n{'='*60}")
    logger.info(f"SWIFT API PIPELINE - EXTRACTIONS ONLY")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    results = {}

    # Organizations & Projects
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting Organizations & Projects...")
        run_orgs_projects_extract()
        results["Organizations & Projects"] = "SUCCESS"
    except Exception as e:
        results["Organizations & Projects"] = f"FAILED: {e}"

    # User Priorities
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting User Priorities...")
        run_user_priorities_extract()
        results["User Priorities"] = "SUCCESS"
    except Exception as e:
        results["User Priorities"] = f"FAILED: {e}"

    # Asset Tasks
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting Asset Tasks...")
        run_asset_task_pipeline()
        results["Asset Tasks"] = "SUCCESS"
    except Exception as e:
        results["Asset Tasks"] = f"FAILED: {e}"

    # QA Forms
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting QA Forms...")
        extract_forms()
        results["QA Forms"] = "SUCCESS"
    except Exception as e:
        results["QA Forms"] = f"FAILED: {e}"

    # Timer
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Extracting Timer Activities...")
        run_timer_pipeline()
        results["Timer Activities"] = "SUCCESS"
    except Exception as e:
        results["Timer Activities"] = f"FAILED: {e}"

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info(f"EXTRACTION SUMMARY")
    logger.info(f"{'='*60}")
    for name, status in results.items():
        logger.info(f"  {name}: {status}")
    logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    return all(status == "SUCCESS" for status in results.values())


def run_all_transformations():
    """Run all transformations only (uses latest successful extractions)"""
    from transform import (
        run_orgs_projects_transform, run_user_priorities_transform,
        run_assets_transform, run_asset_tasks_transform,
        run_qa_forms_transform, run_timer_transform
    )

    logger.info(f"\n{'='*60}")
    logger.info(f"SWIFT API PIPELINE - TRANSFORMATIONS ONLY")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    results = {}

    # Organizations & Projects
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming Organizations & Projects...")
        run_orgs_projects_transform()
        results["Organizations & Projects"] = "SUCCESS"
    except Exception as e:
        results["Organizations & Projects"] = f"FAILED: {e}"

    # User Priorities
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming User Priorities...")
        run_user_priorities_transform()
        results["User Priorities"] = "SUCCESS"
    except Exception as e:
        results["User Priorities"] = f"FAILED: {e}"

    # Assets (from asset tasks raw data)
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming Assets...")
        run_assets_transform()
        results["Assets"] = "SUCCESS"
    except Exception as e:
        results["Assets"] = f"FAILED: {e}"

    # Asset Tasks
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming Asset Tasks...")
        run_asset_tasks_transform()
        results["Asset Tasks"] = "SUCCESS"
    except Exception as e:
        results["Asset Tasks"] = f"FAILED: {e}"

    # QA Forms
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming QA Forms...")
        run_qa_forms_transform()
        results["QA Forms"] = "SUCCESS"
    except Exception as e:
        results["QA Forms"] = f"FAILED: {e}"

    # Timer
    try:
        logger.info(f"\n[{datetime.now():%H:%M:%S}] Transforming Timer Activities...")
        run_timer_transform()
        results["Timer Activities"] = "SUCCESS"
    except Exception as e:
        results["Timer Activities"] = f"FAILED: {e}"

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info(f"TRANSFORMATION SUMMARY")
    logger.info(f"{'='*60}")
    for name, status in results.items():
        logger.info(f"  {name}: {status}")
    logger.info(f"\nCompleted: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    return all(status == "SUCCESS" for status in results.values())


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
        choices=["orgs", "user_priorities", "asset_tasks", "forms", "timer"],
        help="Run a specific pipeline (extract + transform)"
    )

    args = parser.parse_args()

    try:
        if args.extract:
            success = run_all_extractions()
        elif args.transform:
            success = run_all_transformations()
        elif args.pipeline:
            if args.pipeline == "orgs":
                success = run_orgs_projects_pipeline()
            elif args.pipeline == "user_priorities":
                success = run_user_priorities_pipeline()
            elif args.pipeline == "asset_tasks":
                success = run_asset_tasks_pipeline()
            elif args.pipeline == "forms":
                success = run_forms_pipeline()
            elif args.pipeline == "timer":
                success = run_timer_pipeline_full()
            else:
                logger.info(f"Unknown pipeline: {args.pipeline}")
                success = False
        else:
            # Default: run all
            success = run_all_pipelines()

        return 0 if success else 1

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        return 130
    except Exception as e:
        logger.info(f"\n\nPipeline failed with error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
