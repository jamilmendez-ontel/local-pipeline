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


# --- Clean entries table (migration 285) -----------------------------------

def test_clean_entries_table_is_watched_for_blanks():
    from pipeline_health_watcher import BLANK_TABLES
    assert "data_staging.stg_timer_entries_clean" in BLANK_TABLES


def test_clean_entries_jobs_are_watched_by_command_text():
    from pipeline_health_watcher import WATCHED_JOBS
    hourly = "select data_staging.refresh_timer_entries_clean()"
    nightly = ("select data_staging.refresh_timer_entries_clean(timestamptz "
               "'2025-08-31 16:00:00+00', now() + interval '1 day')")
    parity = "select data_staging.snapshot_timer_entries_parity()"
    commands = {"hourly": hourly, "nightly": nightly, "parity": parity}

    def matches(needle):
        # probe_staleness matches with ILIKE '%needle%'.
        return {k for k, c in commands.items() if needle.lower() in c.lower()}

    assert matches(WATCHED_JOBS["timer_entries_clean_hourly"]) == {"hourly"}
    assert matches(WATCHED_JOBS["timer_entries_clean_nightly"]) == {"nightly"}
    assert matches(WATCHED_JOBS["timer_entries_clean_parity"]) == {"parity"}


def test_staleness_uses_a_per_job_threshold_when_given():
    rows = [
        # Hourly job 70 min after its last success: on time, not stale.
        {"job_name": "timer_entries_clean_hourly", "threshold_min": 90,
         "last_success": NOW - timedelta(minutes=70)},
        # Nightly job 20 h after its last success: on time.
        {"job_name": "timer_entries_clean_nightly", "threshold_min": 1560,
         "last_success": NOW - timedelta(hours=20)},
        # Nightly job 30 h after: a night was missed.
        {"job_name": "timer_entries_clean_parity", "threshold_min": 1560,
         "last_success": NOW - timedelta(hours=30)},
    ]
    findings = evaluate_staleness(rows, NOW)
    assert len(findings) == 1
    assert "timer_entries_clean_parity" in findings[0] and "1560" in findings[0]


def test_staleness_override_trips_every_job():
    rows = [{"job_name": "timer_entries_clean_nightly", "threshold_min": 1560,
             "last_success": NOW - timedelta(minutes=5)}]
    assert evaluate_staleness(rows, NOW) == []
    assert len(evaluate_staleness(rows, NOW, override_min=0)) == 1


def test_staleness_rules_cover_the_clean_entries_jobs():
    from pipeline_health_watcher import WATCHED_JOBS, JOB_STALE_MIN
    assert JOB_STALE_MIN["timer_entries_clean_hourly"] > 60
    assert JOB_STALE_MIN["timer_entries_clean_nightly"] > 24 * 60
    assert JOB_STALE_MIN["timer_entries_clean_parity"] > 24 * 60
    assert set(JOB_STALE_MIN) <= set(WATCHED_JOBS)


def test_missing_table_is_a_finding_not_a_crash():
    findings = evaluate_blanks({"data_staging.stg_timer_entries_clean": None,
                                "analytics.mv_timer_day_rollup": 5})
    assert len(findings) == 1
    assert "stg_timer_entries_clean" in findings[0] and "does not exist" in findings[0]
