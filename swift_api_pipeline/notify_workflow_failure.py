"""Email a FAILED notice for a GHA workflow step that has no notifier of its own.

Used by pipeline-asset-tasks.yml's `if: failure()` step, and (since
2026-10-29, --reason cancelled) by its `if: cancelled()` step: a job that hits
timeout-minutes is CANCELLED, not failed, and the asset_tasks step's own
notifier is killed with it, so before this a timeout sent no email at all
(10-29: cancelled 4 min into the transform, staging stayed a day old, silent). The asset_tasks
step itself already emails on failure via run_pipeline_with_notification,
so the workflow only calls this when a LATER step (backfill, analytics MV
refresh, downstream dispatch) failed. Since 2026-09-08 that matters more:
in the 6 PM PHT chain (gmail-scraper -> this workflow -> date-validator) a
silent failure here means the day's validator email never goes out.

Usage:
    python notify_workflow_failure.py --workflow "Pipeline: Asset Tasks" \
        --downstream validator --run-url https://github.com/.../actions/runs/123
Always exits 0 (best-effort alert).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from pipeline_notifier import PipelineResult, send_pipeline_email


def build_result(args, now: datetime) -> PipelineResult:
    if args.reason == "cancelled":
        return PipelineResult(
            pipeline_name=f"{args.workflow} (cancelled / timed out)",
            status="FAILED",
            started_at=now,
            ended_at=now,
            duration_seconds=0.0,
            error_message=(
                "The job was CANCELLED, almost always by its timeout-minutes limit "
                "(or a manual cancel). Whatever step was running did not finish: "
                "data_staging.stg_asset_tasks may still hold the previous load and "
                "the downstream dispatches (export email, Fri/Mon reports, validator) "
                "after it did not fire. Recovery: if asset_tasks_extract shows success "
                "for tonight in pipeline.pipeline_runs, re-run this workflow with "
                "stage=transform (minutes, no API re-pull); otherwise stage=full. "
                f"Run: {args.run_url or 'n/a'}"
            ),
        )
    consequence = (
        "The 6 PM PHT chain (gmail-scraper -> asset tasks -> date-validator) "
        "stopped here: date-validator-daily was NOT dispatched, so today's "
        "validator email will not go out. Recovery: re-run this workflow "
        "(workflow_dispatch, downstream=validator)."
        if args.downstream == "validator"
        else "A post-extract step failed; downstream dispatches after it did not fire."
    )
    return PipelineResult(
        pipeline_name=f"{args.workflow} (post-extract step)",
        status="FAILED",
        started_at=now,
        ended_at=now,
        duration_seconds=0.0,
        error_message=(
            f"A step after the asset_tasks extract/transform failed "
            f"(backfill, analytics MV refresh, or a downstream dispatch). "
            f"{consequence} Run: {args.run_url or 'n/a'}"
        ),
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workflow", required=True)
    p.add_argument("--downstream", default="")
    p.add_argument("--run-url", default="")
    p.add_argument("--reason", choices=("failed", "cancelled"), default="failed")
    args = p.parse_args(argv)

    now = datetime.now(timezone.utc)
    result = build_result(args, now)
    try:
        send_pipeline_email(
            results=[result],
            log_output=f"downstream mode: {args.downstream or 'all'}\nrun: {args.run_url}\n",
            overall_status="FAILED",
            run_label=(f"{args.workflow} [{args.downstream or 'all'}]"
                       + (" CANCELLED/TIMEOUT" if args.reason == "cancelled" else "")),
            started_at=now,
            ended_at=now,
            total_duration=0.0,
        )
    except Exception as e:  # never fail the alert step itself
        print(f"notify_workflow_failure: email send failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
