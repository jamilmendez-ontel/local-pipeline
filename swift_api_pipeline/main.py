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
import argparse
from datetime import datetime
from config import setup_logging, get_logger

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# Initialize logging for all pipeline modules
setup_logging()
logger = get_logger("main")


def run_orgs_projects_pipeline():
    """Run organizations and projects extraction + transformation"""
    from pipeline import run_pipeline
    from transform import run_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ORGANIZATIONS & PROJECTS PIPELINE")
    logger.info(f"{'#'*60}")

    # Extract (returns 0 on success)
    result = run_pipeline()
    if result != 0:
        raise RuntimeError("Organizations/Projects extraction failed")

    # Transform uses latest successful run
    run_transform()

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

    # Transform assets (aggregated from asset tasks)
    run_assets_transform(run_id)

    # Transform asset tasks (individual task records)
    run_asset_tasks_transform(run_id)

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

    # Transform
    run_qa_forms_transform(run_id)

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

    # Transform
    run_timer_transform(run_id)

    return True


def run_all_pipelines():
    """Run all pipelines in sequence"""
    logger.info(f"\n{'='*60}")
    logger.info(f"SWIFT API PIPELINE - FULL RUN")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    pipelines = [
        ("Organizations & Projects", run_orgs_projects_pipeline),
        ("Asset Tasks", run_asset_tasks_pipeline),
        ("QA Forms", run_forms_pipeline),
        ("Timer Activities", run_timer_pipeline_full),
    ]

    results = {}

    for name, func in pipelines:
        try:
            logger.info(f"\n[{datetime.now():%H:%M:%S}] Starting: {name}")
            func()
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
    from pipeline import run_pipeline
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
        run_pipeline()
        results["Organizations & Projects"] = "SUCCESS"
    except Exception as e:
        results["Organizations & Projects"] = f"FAILED: {e}"

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
        run_transform, run_assets_transform, run_asset_tasks_transform,
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
        run_transform()
        results["Organizations & Projects"] = "SUCCESS"
    except Exception as e:
        results["Organizations & Projects"] = f"FAILED: {e}"

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
  python main.py                         # Run all pipelines (extract + transform)
  python main.py --extract               # Run all extractions only
  python main.py --transform             # Run all transformations only
  python main.py --pipeline asset_tasks  # Run asset_tasks pipeline only
  python main.py --pipeline forms        # Run QA forms pipeline only
  python main.py --pipeline timer        # Run timer pipeline only
  python main.py --pipeline orgs         # Run orgs/projects pipeline only
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
        choices=["orgs", "asset_tasks", "forms", "timer"],
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
