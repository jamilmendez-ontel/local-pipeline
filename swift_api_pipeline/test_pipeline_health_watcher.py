# swift_api_pipeline/test_pipeline_health_watcher.py
from datetime import datetime, timedelta, timezone

from pipeline_health_watcher import (
    evaluate_failed_runs,
    evaluate_staleness,
    evaluate_blanks,
    evaluate_rebuild_duration,
    build_email_body,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def test_failed_runs_reported_with_counts_and_message():
    rows = [
        {"jobid": 9, "command": "SELECT analytics.refresh_dr_task_rollup_safe()",
         "status": "failed", "return_message": "deadlock detected"},
        {"jobid": 9, "command": "SELECT analytics.refresh_dr_task_rollup_safe()",
         "status": "failed", "return_message": "deadlock detected"},
    ]
    findings = evaluate_failed_runs(rows)
    assert len(findings) == 1
    assert "jobid 9" in findings[0] and "2 failed" in findings[0] and "deadlock detected" in findings[0]


def test_failed_runs_empty_when_all_succeeded():
    assert evaluate_failed_runs([]) == []


def test_staleness_flags_old_success_and_missing_job():
    rows = [
        {"job_name": "dr_task_rollup", "last_success": NOW - timedelta(minutes=45)},
        {"job_name": "timer_day_rollup", "last_success": NOW - timedelta(minutes=5)},
    ]
    findings = evaluate_staleness(rows, NOW)
    assert len(findings) == 1
    assert "dr_task_rollup" in findings[0] and "45" in findings[0]
    # A job with NO successful run at all is its own finding:
    findings2 = evaluate_staleness([{"job_name": "dr_task_rollup", "last_success": None}], NOW)
    assert len(findings2) == 1 and "no successful run" in findings2[0]


def test_blanks_flag_zero_rows_only():
    counts = {
        "data_staging.stg_timer_activities_clean": 385675,
        "analytics.mv_timer_day_rollup": 0,
        "analytics.mv_hr_report_review": 26352,
    }
    findings = evaluate_blanks(counts)
    assert len(findings) == 1 and "mv_timer_day_rollup" in findings[0] and "0 rows" in findings[0]
    assert evaluate_blanks({"a": 1, "b": 2}) == []


def test_rebuild_duration_threshold_is_strict():
    assert evaluate_rebuild_duration(120000) == []
    assert len(evaluate_rebuild_duration(120001)) == 1
    assert "120001" in evaluate_rebuild_duration(120001)[0]
    assert evaluate_rebuild_duration(None) == []  # no stats row = nothing to report


def test_email_body_lists_every_finding():
    body = build_email_body(["finding one", "finding two"], NOW)
    assert "finding one" in body and "finding two" in body
    assert "2026-08-04" in body
    # NOW is 12:00 UTC; displayed timestamp converts to America/New_York (08:00 AM ET)
    assert "08:00 AM ET" in body
