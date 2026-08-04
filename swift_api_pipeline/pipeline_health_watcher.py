#!/usr/bin/env python3
"""Pipeline health watcher: detect silent warehouse failures and email Jamil.

Checks (spec 2026-08-04, thresholds are spec-fixed):
  1. cron.job_run_details rows with status <> 'succeeded' in the last 24h.
  2. The 5-min DR refresh chain and the 10-min timer rollup refresh: last
     SUCCESSFUL run older than 30 minutes (jobs located by command text, never
     by jobid; jobids change when jobs are recreated).
  3. Zero rows on stg_timer_activities_clean / mv_timer_day_rollup /
     mv_hr_report_review (the stale-not-blank guards' worst case).
  4. rebuild_timer_clean() max_exec_time above 120s (headroom alarm before its
     300s statement_timeout; migration 218 made it non-blocking, not fast).

Behavior: healthy run = one log line, NO email, exit 0. Any finding = one
plain-text email to Jamil listing all findings, exit 0. Watcher crash = exit 1
(GitHub's own workflow-failure notification covers watcher self-death).

Usage:
    python pipeline_health_watcher.py               # check + email findings
    python pipeline_health_watcher.py --dry-run     # check + print, never email
    python pipeline_health_watcher.py --force-findings  # treat every check's
        threshold as tripped where possible (staleness 0 min, duration 0 ms) to
        exercise the email path end-to-end
"""

import argparse
import base64
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

from config import get_logger, get_db, close_db, retry_db, setup_logging

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

setup_logging()
logger = get_logger("pipeline_health_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
STALE_THRESHOLD_MIN = 30
REBUILD_ALARM_MS = 120_000
BLANK_TABLES = [
    "data_staging.stg_timer_activities_clean",
    "analytics.mv_timer_day_rollup",
    "analytics.mv_hr_report_review",
]
# Located by command text (jobids change when jobs are recreated). Needles
# must match cron.job.command text (verified live 2026-08-04: jobid 9 =
# SELECT analytics.refresh_dr_task_rollup_safe(); jobid 11 =
# SELECT analytics.refresh_timer_day_rollup_safe()).
WATCHED_JOBS = {
    "dr_task_rollup": "refresh_dr_task_rollup_safe",
    "timer_day_rollup": "refresh_timer_day_rollup_safe",
}


# ---------------------------------------------------------------------------
# Pure evaluators (unit-tested; no DB access)
# ---------------------------------------------------------------------------

def evaluate_failed_runs(rows):
    """rows: [{jobid, command, status, return_message}] for the last 24h,
    already filtered to status <> 'succeeded'. One finding per jobid."""
    by_job = {}
    for r in rows:
        by_job.setdefault(r["jobid"], []).append(r)
    findings = []
    for jobid, items in sorted(by_job.items()):
        head = (items[0]["command"] or "").strip()[:60]
        msg = (items[0].get("return_message") or "").strip()[:200]
        findings.append(
            f"cron jobid {jobid} ({head}): {len(items)} failed run(s) in the "
            f"last 24h; most recent message: {msg}"
        )
    return findings


def evaluate_staleness(rows, now, threshold_min=STALE_THRESHOLD_MIN):
    """rows: [{job_name, last_success: datetime|None}]."""
    findings = []
    for r in rows:
        if r["last_success"] is None:
            findings.append(
                f"refresh job {r['job_name']}: no successful run found in "
                f"cron.job_run_details at all"
            )
            continue
        age_min = (now - r["last_success"]).total_seconds() / 60.0
        if age_min > threshold_min:
            findings.append(
                f"refresh job {r['job_name']}: last SUCCESSFUL run was "
                f"{age_min:.0f} minutes ago (threshold {threshold_min}m); "
                f"DR Monitoring / Hours Variance are serving stale data"
            )
    return findings


def evaluate_blanks(counts):
    """counts: {qualified_table_name: row_count}."""
    return [
        f"{table}: 0 rows; a rebuild or refresh ran from an empty source "
        f"(stale-not-blank guard bypassed)"
        for table, n in counts.items()
        if n == 0
    ]


def evaluate_rebuild_duration(max_ms, threshold_ms=REBUILD_ALARM_MS):
    """max_ms: max_exec_time for rebuild_timer_clean from pg_stat_statements,
    None when no stats row exists (fresh stats reset)."""
    if max_ms is None or max_ms <= threshold_ms:
        return []
    return [
        f"rebuild_timer_clean(): max exec time {max_ms:.0f} ms exceeds the "
        f"{threshold_ms} ms alarm (statement_timeout is 300000 ms); the "
        f"outlier is becoming the norm"
    ]


def build_email_body(findings, checked_at):
    lines = [
        "Pipeline health watcher findings",
        f"Checked at: {checked_at:%Y-%m-%d %H:%M %Z}",
        "",
    ]
    lines += [f"  {i}. {f}" for i, f in enumerate(findings, 1)]
    lines += [
        "",
        "Runbook: cron history -> select * from cron.job_run_details order by "
        "runid desc limit 50; refresh chain = analytics.refresh_dr_task_rollup_safe(); "
        "rollup = analytics.refresh_timer_day_rollup_safe(); rebuild source guard "
        "lives in timer_correction_review.rebuild_clean_table().",
        "",
        "(pipeline_health_watcher.py; silent when healthy)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB probes (thin; each returns the pure evaluator's input shape)
# ---------------------------------------------------------------------------

def probe_failed_runs(db):
    rows = retry_db(lambda: db.fetch(
        "SELECT jobid, command, status, return_message "
        "FROM cron.job_run_details "
        "WHERE status <> 'succeeded' AND runid IN ("
        "  SELECT runid FROM cron.job_run_details "
        "  WHERE COALESCE(start_time, now()) > now() - interval '24 hours' "
        ") ORDER BY runid DESC LIMIT 200"
    ), description="failed cron runs")
    return [dict(r) for r in rows]


def probe_staleness(db):
    out = []
    for job_name, needle in WATCHED_JOBS.items():
        row = retry_db(lambda n=needle: db.fetchrow(
            "SELECT max(d.start_time) AS last_success "
            "FROM cron.job_run_details d "
            "JOIN cron.job j ON j.jobid = d.jobid "
            "WHERE d.status = 'succeeded' AND j.command ILIKE '%' || $1 || '%'",
            n
        ), description=f"staleness {job_name}")
        out.append({"job_name": job_name,
                    "last_success": row["last_success"] if row else None})
    return out


def probe_blanks(db):
    counts = {}
    for table in BLANK_TABLES:
        row = retry_db(lambda t=table: db.fetchrow(f"SELECT count(*) AS n FROM {t}"),
                       description=f"count {table}")
        counts[table] = int(row["n"])
    return counts


def probe_rebuild_duration(db):
    try:
        row = db.fetchrow(
            "SELECT max(max_exec_time) AS max_ms FROM extensions.pg_stat_statements "
            "WHERE query ILIKE '%rebuild_timer_clean()%' AND query NOT ILIKE '%pg_stat%'"
        )
        return float(row["max_ms"]) if row and row["max_ms"] is not None else None
    except Exception as e:  # pg_stat_statements location/permission drift is
        # itself worth knowing about, but not worth failing the whole watcher.
        logger.warning(f"pg_stat_statements probe failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Email (same Gmail API pattern as pipeline_notifier / gmail_client)
# ---------------------------------------------------------------------------

def send_email(findings, checked_at):
    from gmail_client import authenticate
    service = authenticate()
    msg = MIMEText(build_email_body(findings, checked_at), "plain")
    msg["To"] = ", ".join(RECIPIENTS)
    msg["From"] = "me"
    msg["Subject"] = f"Pipeline health: {len(findings)} finding(s)"
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Findings email sent to {', '.join(RECIPIENTS)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print findings, never email")
    parser.add_argument("--force-findings", action="store_true",
                        help="zero thresholds to exercise the email path")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    stale_threshold = 0 if args.force_findings else STALE_THRESHOLD_MIN
    rebuild_threshold = 0 if args.force_findings else REBUILD_ALARM_MS

    db = get_db()
    try:
        findings = []
        findings += evaluate_failed_runs(probe_failed_runs(db))
        findings += evaluate_staleness(probe_staleness(db), now, stale_threshold)
        findings += evaluate_blanks(probe_blanks(db))
        findings += evaluate_rebuild_duration(probe_rebuild_duration(db), rebuild_threshold)
    finally:
        close_db()

    if not findings:
        logger.info("All pipeline health checks green; no email.")
        return 0

    logger.warning(f"{len(findings)} finding(s):")
    for f in findings:
        logger.warning(f"  - {f}")
    if args.dry_run:
        logger.info("Dry run: email suppressed.")
    else:
        send_email(findings, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
