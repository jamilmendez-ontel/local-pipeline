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

import os
import sys
import time
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import setup_logging, get_logger
from db import close_db
from pipeline_notifier import PipelineResult, PipelineOutcome, PIPELINE_TABLES, ALL_TABLES, capture_logs, send_pipeline_email, snapshot_row_counts

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
    "asset_tasks_extract": "Asset Tasks Extract",
    "asset_tasks_transform": "Asset Tasks Transform",
    "asset_tasks_gc": "Asset Tasks GC",
    "asset_tasks_gc_extract": "Asset Tasks GC Extract",
    "asset_tasks_gc_transform": "Asset Tasks GC Transform",
    "targeted_asset_tasks": "Targeted Asset Tasks",
    "targeted_task_requirements": "Targeted Task Requirements",
    "analytics_gc": "Analytics GC MV Refresh",
    "forms": "QA Forms",
    "invoicing": "Invoicing Form",
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
    """Run user priorities extraction + transformation.

    Snapshot-aware: when the fetched snapshot is byte-identical to the last
    loaded one, the extract returns None and the full delete+reload of raw
    AND staging (~23.5k row writes at the 5-minute cadence) is skipped.
    Freshness is unchanged: any real change still loads within one run.
    The watermark advances only after a successful transform.
    """
    from pipeline import run_user_priorities_extract
    from transform import run_user_priorities_transform
    from config import SCHEMA_PIPELINE, get_db, retry_db

    logger.info(f"\n{'#'*60}")
    logger.info(f"# USER PRIORITIES PIPELINE")
    logger.info(f"{'#'*60}")

    result = run_user_priorities_extract(skip_if_unchanged=True)
    if result is None:
        logger.info("Snapshot unchanged since last load: transform skipped")
        return True

    run_id, snapshot_hash = result
    run_user_priorities_transform(run_id)

    retry_db(
        lambda: get_db().execute(
            f"INSERT INTO {SCHEMA_PIPELINE}.content_watermarks "
            f"(pipeline_name, content_hash, updated_at) VALUES ($1, $2, now()) "
            f"ON CONFLICT (pipeline_name) DO UPDATE SET "
            f"content_hash = EXCLUDED.content_hash, updated_at = now()",
            "user_priorities", snapshot_hash,
        ),
        description="advance user_priorities content watermark",
    )

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

    outcome = run_asset_task_pipeline(project_filter=project_filter)

    # Transform assets (aggregated from asset tasks)
    run_assets_transform(outcome.run_id)

    # Transform asset tasks (individual task records)
    run_asset_tasks_transform(outcome.run_id)

    # After single-project recovery, also update backfill and analytics
    if project_filter:
        from transform import backfill_asset_did, refresh_analytics
        logger.info("Recovery: running backfill_asset_did and analytics refresh...")
        backfill_asset_did()
        refresh_analytics()

    return outcome


def run_asset_tasks_extract_pipeline():
    """Run asset tasks EXTRACT only (Swift API -> raw_asset_tasks).

    Split from transform so a transform-only failure doesn't waste the ~60-min API pull.
    Companion: run_asset_tasks_transform_pipeline reads run_id from pipeline.pipeline_runs.
    """
    from extract_asset_tasks import run_asset_task_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS EXTRACT")
    logger.info(f"{'#'*60}")

    return run_asset_task_pipeline()


def run_asset_tasks_transform_pipeline():
    """Run asset tasks TRANSFORM only (raw_asset_tasks -> stg_assets + stg_asset_tasks).

    Looks up the latest successful asset_tasks_extract run_id from pipeline.pipeline_runs.
    """
    from transform import run_assets_transform, run_asset_tasks_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS TRANSFORM")
    logger.info(f"{'#'*60}")

    run_assets_transform()
    run_asset_tasks_transform()
    return True


def run_asset_tasks_gc_pipeline():
    """Run GC asset tasks combined extract + inline transforms.

    Calls extract_asset_tasks_gc.run_asset_task_gc_pipeline which performs
    extract + inline run_assets_gc_transform + run_asset_tasks_gc_transform.
    """
    from extract_asset_tasks_gc import run_asset_task_gc_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC PIPELINE")
    logger.info(f"{'#'*60}")

    run_asset_task_gc_pipeline()
    return True


def run_asset_tasks_gc_extract_pipeline():
    """Run GC asset_tasks EXTRACT only (Swift API -> raw_asset_tasks_gc).

    v1 implementation note: currently aliases the full pipeline (extract +
    inline transforms). Splitting is YAGNI until a use case for
    extract-only emerges.
    """
    from extract_asset_tasks_gc import run_asset_task_gc_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC EXTRACT (aliases full GC pipeline)")
    logger.info(f"{'#'*60}")

    run_asset_task_gc_pipeline()
    return True


def run_asset_tasks_gc_transform_pipeline():
    """Run GC asset_tasks TRANSFORM only.

    Looks up the latest successful asset_tasks_gc_extract run_id and runs
    the SQL aggregation + Python-driven INSERT-SELECT pair.
    """
    from transform import run_assets_gc_transform, run_asset_tasks_gc_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC TRANSFORM")
    logger.info(f"{'#'*60}")

    run_assets_gc_transform()
    run_asset_tasks_gc_transform()
    return True


def run_targeted_asset_tasks_pipeline_wrapper():
    """Run the targeted asset_tasks pipeline.

    Lighter walk than the GC pipeline (uses /api/projects/{p}/assets +
    /api/asset-projects/{a}/asset-tasks instead of the heavy _export
    endpoint). Reads targets from reference.report_targets, writes to
    data_staging.stg_targeted_asset_tasks with TRUNCATE-and-reload
    semantics per report_name.

    Optional CLI arg `--report-name X` filters to that one report.
    """
    from extract_targeted_asset_tasks import run_targeted_asset_tasks_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# TARGETED ASSET TASKS PIPELINE")
    logger.info(f"{'#'*60}")

    report_name = os.environ.get("REPORT_NAME") or None
    return run_targeted_asset_tasks_pipeline(report_name=report_name)


def run_targeted_task_requirements_pipeline_wrapper():
    """Run the targeted task-requirements pipeline.

    Reads tasks from data_staging.stg_user_priorities filtered by:
      - org_did/project_did from enabled reference.report_targets rows
      - task_name ILIKE '%punch%'
      - status IN ('pending', 'in_progress')
      - assigned_to IS NOT NULL
    For each task_did, fetches requirements via /api/asset-tasks/{task_did}/requirements
    and keeps only requirements with status in (pending, in_progress).
    Writes to data_staging.stg_targeted_task_requirements with TRUNCATE-and-reload
    semantics per report_name.

    Optional CLI env `REPORT_NAME=X` filters to that one report.
    """
    from extract_targeted_task_requirements import run_targeted_task_requirements_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# TARGETED TASK REQUIREMENTS PIPELINE")
    logger.info(f"{'#'*60}")

    report_name = os.environ.get("REPORT_NAME") or None
    return run_targeted_task_requirements_pipeline(report_name=report_name)


def run_analytics_gc_refresh():
    """Refresh the three _gc analytics MVs (mv_project_summary_gc, etc).

    Ontel MVs are untouched by this refresh.
    """
    from transform import refresh_analytics_gc

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ANALYTICS GC MV REFRESH")
    logger.info(f"{'#'*60}")

    refresh_analytics_gc()
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


def run_invoicing_pipeline():
    """Run invoicing form extraction + transformation (Quote Automation)."""
    from extract_invoicing_form import run_invoicing_extract
    from transform import run_invoicing_transform, refresh_quote_mvs

    logger.info(f"\n{'#'*60}")
    logger.info(f"# INVOICING FORM PIPELINE")
    logger.info(f"{'#'*60}")

    run_id = run_invoicing_extract()
    run_invoicing_transform(run_id)

    # Invoice data drives both quote MVs; refresh so the app reflects new
    # pricing/lines immediately after an invoicing reload.
    refresh_quote_mvs()

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
    from transform import enrich_stg_assets_with_status, seed_market_signatures

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSETS EXTRACT + STATUS ENRICHMENT")
    logger.info(f"{'#'*60}")

    run_assets_extract()
    enrich_stg_assets_with_status()
    seed_market_signatures()
    return True


def run_analytics_refresh():
    """Refresh analytics materialized views (core + Quote Automation).

    The nightly asset-tasks GHA workflow runs this via `--pipeline analytics`
    after the worklist (stg_asset_tasks) reloads, so the quote MVs pick up
    worklist/override/directory changes daily alongside the three core MVs.
    """
    from transform import refresh_analytics, refresh_quote_mvs

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ANALYTICS MV REFRESH")
    logger.info(f"{'#'*60}")

    refresh_analytics()
    refresh_quote_mvs()
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


def run_pipeline_with_notification(func, name, send_email=True, logger_prefixes=None, recipients=None, email_on_success=True):
    """Run a single pipeline with log capture and email notification.

    email_on_success=False suppresses the SUCCESS email (failure emails are
    still sent whenever send_email is True). Used for high-frequency pipelines
    (e.g. user_priorities every 10 min) where a success email every run is noise.
    """
    # Counts exist only for the notification emails: skip them entirely when
    # no email can be sent, and use cheap reltuples estimates for
    # high-frequency runs (email_on_success=False) where they'd only ever
    # show in a failure email. Exact COUNT(*) full-scans every table twice
    # per run, which at a 5-minute cadence was a main driver of the Supabase
    # Disk IO budget depletion (2026-07-09).
    tables = PIPELINE_TABLES.get(name) if send_email else None
    estimate_counts = not email_on_success
    started_at = datetime.now(timezone.utc)
    row_counts_before = snapshot_row_counts(tables, estimate=estimate_counts)
    with capture_logs(logger_prefixes=logger_prefixes) as log_handler:
        try:
            outcome = func()
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables, estimate=estimate_counts)

            if isinstance(outcome, PipelineOutcome) and not outcome.is_clean():
                overall = outcome.email_status()        # PARTIAL FAILURE | ABNORMAL ROW COUNT
                result = PipelineResult(
                    pipeline_name=name,
                    status=overall,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                    error_message=outcome.detail,
                )
                if send_email:                          # degraded is never routine: ignore email_on_success
                    send_pipeline_email(
                        results=[result],
                        log_output=log_handler.get_log_output(),
                        overall_status=overall,
                        run_label=name,
                        started_at=started_at,
                        ended_at=ended_at,
                        total_duration=duration,
                        recipients=recipients,
                        row_counts_before=row_counts_before,
                        row_counts_after=row_counts_after,
                        row_count_tables=tables,
                    )
                return True   # exit 0 so good projects' transforms/downstream still run

            result = PipelineResult(
                pipeline_name=name,
                status="SUCCESS",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration,
            )
            if send_email and email_on_success:
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
            row_counts_after = snapshot_row_counts(tables, estimate=estimate_counts)
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
        # NOTE: this aggregate-summary path reports SUCCESS as long as func()
        # did not raise; it does NOT inspect a returned PipelineOutcome, so a
        # degraded (partial/abnormal) asset_tasks run would still show green in
        # the run_all_* summary. The per-pipeline degraded email still fires
        # inside run_pipeline_with_notification. The scheduled asset-tasks
        # workflow does NOT use this path (it calls --pipeline asset_tasks*
        # directly via run_pipeline_with_notification), so the honesty guarantee
        # holds in production. Fixing this fully would require the wrapper to
        # return the outcome status, not just True.
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

    # Downstream: notify date-validator that fresh asset_tasks data is ready.
    # Never fail the pipeline on a dispatch error.
    if overall_success:
        try:
            from github_trigger import fire_dispatch
            fire_dispatch(
                "jamilmendez-ontel/date-validator",
                "date-validator-daily",
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
                # NOTE: called directly (not via run_pipeline_with_notification), so a
                # degraded PipelineOutcome here is NOT surfaced as a red email or a
                # non-green summary on this aggregate path. The scheduled asset-tasks
                # job does not use run_all_extractions, so production stays honest.
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
        choices=["orgs", "user_priorities", "asset_tasks", "asset_tasks_extract", "asset_tasks_transform", "asset_tasks_gc", "asset_tasks_gc_extract", "asset_tasks_gc_transform", "targeted_asset_tasks", "targeted_task_requirements", "analytics_gc", "forms", "invoicing", "timer", "aging", "sales", "backfill", "analytics", "assets"],
        help="Run a specific pipeline (extract + transform)"
    )

    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Suppress email notifications after pipeline run"
    )
    parser.add_argument(
        "--email-on-failure-only",
        action="store_true",
        help="Only send an email if the pipeline FAILS (suppress success emails). "
             "Used for high-frequency runs like user_priorities every 10 min."
    )
    parser.add_argument(
        "--project",
        type=str,
        metavar="TS16",
        help="Recover a single project (use with --pipeline asset_tasks only). E.g. --project TS16"
    )

    args = parser.parse_args()
    send_email = not args.no_email
    email_on_success = not args.email_on_failure_only

    if args.project and args.pipeline != "asset_tasks":
        parser.error("--project can only be used with --pipeline asset_tasks")

    # Map pipeline names to functions
    pipeline_funcs = {
        "orgs": run_orgs_projects_pipeline,
        "user_priorities": run_user_priorities_pipeline,
        "asset_tasks": run_asset_tasks_pipeline,
        "asset_tasks_extract": run_asset_tasks_extract_pipeline,
        "asset_tasks_transform": run_asset_tasks_transform_pipeline,
        "asset_tasks_gc": run_asset_tasks_gc_pipeline,
        "asset_tasks_gc_extract": run_asset_tasks_gc_extract_pipeline,
        "asset_tasks_gc_transform": run_asset_tasks_gc_transform_pipeline,
        "targeted_asset_tasks": run_targeted_asset_tasks_pipeline_wrapper,
        "targeted_task_requirements": run_targeted_task_requirements_pipeline_wrapper,
        "analytics_gc": run_analytics_gc_refresh,
        "forms": run_forms_pipeline,
        "invoicing": run_invoicing_pipeline,
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
            success = run_pipeline_with_notification(func, name, send_email=send_email, email_on_success=email_on_success)
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
