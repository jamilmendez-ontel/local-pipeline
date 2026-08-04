# Non-Blocking Timer-Clean Rebuild + Pipeline Health Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `rebuild_timer_clean()` from blocking readers (migration 218: TRUNCATE -> DELETE), add a daily email watcher for refresh failures/staleness/blank MVs/rebuild duration, and retry-wrap the two ontel-people auth reads that turned the last brownout into an error page.

**Architecture:** Two PRs. PR A (local-pipeline, worktree `C:\Users\admin\Desktop\Projects\ai-projects\.worktrees\lp-health`, branch `feat/timer-clean-nonblocking-and-watcher`): migration 218 + `pipeline_health_watcher.py` + GHA workflow. PR B (ontel-people): `withDbRetry` around the `getSession` lookups, merged only after Jamil's localhost gate. The controller applies the migration and runs the live non-blocking proof.

**Tech Stack:** PostgreSQL (Supabase), plpgsql, Python 3.12 (pipeline conventions: `config.get_db/retry_db/get_logger`, `gmail_client.authenticate`), GitHub Actions, Next.js (`lib/hr/db-retry.ts`).

**Spec:** `docs/superpowers/specs/2026-08-04-timer-clean-nonblocking-and-watcher-design.md` (this worktree).

## Global Constraints

- Migration number is **218** (dir tops out at 217 on origin/main; re-verify with `ls swift_api_pipeline/migrations | grep -E '^21'` before committing, other sessions ship fast).
- The migration changes ONLY the `TRUNCATE` line of `rebuild_timer_clean()` to `DELETE FROM`; every other statement of the live body stays byte-identical. The live body MUST be captured from `pg_get_functiondef` at apply time and diffed against the plan's embedded body; abort on drift.
- Watcher alerts: email ONLY (Jamil's decision), recipients `["jamil.mendez@ontel.co"]` only (standing email rule). Silent when healthy.
- `cron.job_run_details` queries order by `runid DESC`, never `start_time` (failed rows carry NULL start_time).
- Watcher locates cron jobs by command text, never hardcoded jobid.
- Thresholds (spec-fixed): staleness 30 minutes for both refresh jobs; rebuild duration alarm at 120,000 ms max_exec_time; blank check = zero rows on `data_staging.stg_timer_activities_clean`, `analytics.mv_timer_day_rollup`, `analytics.mv_hr_report_review`.
- PR B merges ONLY after Jamil verifies on localhost (standing rule, memory: localhost-gate-before-merge). PR A is pipeline-only and has no localhost gate.
- Scheduling deviation from spec, chosen deliberately: the workflow carries a GHA `schedule:` cron (daily 18:00 UTC) PLUS `repository_dispatch` + `workflow_dispatch`, following the roster-gap-watcher precedent (LIVE on GHA cron since 2026-07-05). The Apps Script dispatch trigger from the spec can be added later without workflow changes; touching the dispatch Apps Script + PAT surface is not worth it for a non-critical daily check. Surface this deviation to Jamil in the ship summary.
- No em-dash characters in any copy.

---

# PR A - local-pipeline

### Task 1: Migration 218 file

**Files:**
- Create: `swift_api_pipeline/migrations/218_rebuild_timer_clean_nonblocking.sql`

**Interfaces:**
- Produces: the same function name/signature `data_staging.rebuild_timer_clean()`; behavior change is lock class only. Task 2 applies it; Task 3's watcher check 4 guards its duration.

- [ ] **Step 1: Write the file.** The body below is the live `pg_get_functiondef` output captured 2026-08-04 (00:30 ET) with exactly one change (TRUNCATE -> DELETE). Full content:

```sql
-- 218: Make rebuild_timer_clean() non-blocking for readers (Jamil 2026-08-04).
-- Spec: docs/superpowers/specs/2026-08-04-timer-clean-nonblocking-and-watcher-design.md
--
-- Incident 2026-08-04 ~00:26 ET: the nightly pipeline-timer run's rebuild took
-- 138.8s; its TRUNCATE held AccessExclusiveLock on stg_timer_activities_clean
-- for the whole transaction, queued readers exhausted the PostgREST pool, and
-- every API endpoint 503'd for ~40s (second brownout of this class; first was
-- 2026-07-31). TRUNCATE blocks even SELECT. DELETE takes RowExclusiveLock:
-- readers keep seeing the complete pre-rebuild table (MVCC snapshot) until the
-- transaction commits, then atomically see the new one. Readers never see a
-- partial table in either design (single transaction).
--
-- Cost accepted: DELETE writes WAL per row and leaves ~179 MB of dead tuples
-- per cycle for autovacuum (385,675 rows at capture time); rebuild mean was
-- 41.1s and stays well inside the function's 300s timeout. The health watcher
-- (pipeline_health_watcher.py, this branch) alarms at 120s max_exec_time.
--
-- ONLY the TRUNCATE line changed. Everything else is the live body verbatim
-- (captured via pg_get_functiondef 2026-08-04; the step-0 preflight aborts on
-- drift). Rollback: re-apply the same body with DELETE swapped back to
-- TRUNCATE (see footer).

-- ---------------------------------------------------------------------------
-- 0) Preflight: abort if the live function drifted from the captured body or
--    218 is already applied.
-- ---------------------------------------------------------------------------
DO $$
DECLARE def text;
BEGIN
  def := pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure);
  IF position('TRUNCATE TABLE data_staging.stg_timer_activities_clean' IN def) = 0 THEN
    IF position('DELETE FROM data_staging.stg_timer_activities_clean;' IN def) > 0 THEN
      RAISE EXCEPTION '218: rebuild_timer_clean already non-blocking; migration appears applied';
    END IF;
    RAISE EXCEPTION '218: live rebuild_timer_clean has no TRUNCATE line; body drifted, re-capture before applying';
  END IF;
  IF position('duplicate_reviews' IN def) = 0 OR position('entry_removals' IN def) = 0 THEN
    RAISE EXCEPTION '218: live rebuild_timer_clean missing expected anti-joins; body drifted, re-capture before applying';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
BEGIN
    -- 218: DELETE, not TRUNCATE. RowExclusiveLock lets concurrent readers see
    -- the pre-rebuild snapshot instead of queuing on AccessExclusiveLock.
    DELETE FROM data_staging.stg_timer_activities_clean;

    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = COALESCE((rej->>'start_time')::timestamptz, r.start_time)
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (e->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.entry_removals rm
            WHERE t.project_did = rm.project_did
              AND t.user_email  = rm.user_email
              AND t.start_time  = rm.start_time
              AND t.site_name IS NOT DISTINCT FROM rm.site_name
              AND t.site_id   IS NOT DISTINCT FROM rm.site_id
              AND t.task      IS NOT DISTINCT FROM rm.task
              AND t.end_time IS NOT DISTINCT FROM rm.end_time
              AND t.duration_min IS NOT DISTINCT FROM rm.duration_min
              AND rm.reason IS DISTINCT FROM 'REVERTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_timer.corrections c
                  WHERE c.project_did = rm.project_did
                    AND c.user_email  = rm.user_email
                    AND c.start_time  = rm.start_time
                    AND c.site_name IS NOT DISTINCT FROM rm.site_name
                    AND c.site_id   IS NOT DISTINCT FROM rm.site_id
                    AND c.task      IS NOT DISTINCT FROM rm.task
                    AND c.end_time IS NOT DISTINCT FROM rm.end_time
                    AND c.original_duration_min IS NOT DISTINCT FROM rm.duration_min
              )
        )
    ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
             t.task, t.end_time, t.duration_min, t.id;

    UPDATE data_staging.stg_timer_activities_clean t
    SET duration_min = c.corrected_duration_min,
        end_time    = c.corrected_end_time
    FROM app_timer.corrections c
    WHERE c.status = 'corrected'
      AND t.project_did = c.project_did
      AND t.user_email  = c.user_email
      AND t.start_time  = c.start_time
      AND t.site_name IS NOT DISTINCT FROM c.site_name
      AND t.site_id   IS NOT DISTINCT FROM c.site_id
      AND t.task      IS NOT DISTINCT FROM c.task
      AND t.end_time IS NOT DISTINCT FROM c.end_time
      AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min;

    INSERT INTO data_staging.stg_timer_activities_clean (
        id, project, project_number, project_did, site_name, site_id,
        task, task_clean, site_lat, site_long, user_lat, user_long,
        user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT
        a.id, a.project, a.project_number, a.project_did, a.site_name, a.site_id,
        a.task, a.task_clean, a.site_lat, a.site_long, a.user_lat, a.user_long,
        a.user_accuracy_m, a.site_vs_user_km, a.start_time, a.end_time, a.duration_min,
        a.user_name, a.user_email, a.user_role,
        a.run_id, a.run_date,
        COALESCE(a.start_date, (a.start_time AT TIME ZONE 'America/New_York')::date),
        COALESCE(a.end_date,   (a.start_time AT TIME ZONE 'America/New_York')::date),
        a.loaded_at
    FROM app_timer.entry_additions a
    WHERE NOT EXISTS (
        SELECT 1 FROM app_timer.entry_removals rm
        WHERE rm.project_did = a.project_did
          AND rm.user_email  = a.user_email
          AND rm.start_time  = a.start_time
          AND rm.site_name IS NOT DISTINCT FROM a.site_name
          AND rm.site_id   IS NOT DISTINCT FROM a.site_id
          AND rm.task      IS NOT DISTINCT FROM a.task
          AND rm.end_time IS NOT DISTINCT FROM a.end_time
          AND rm.duration_min IS NOT DISTINCT FROM a.duration_min
          AND rm.reason IS DISTINCT FROM 'REVERTED'
    );

    INSERT INTO data_staging.stg_timer_activities_clean (
        project, project_number, project_did, site_name, site_id,
        task, task_clean, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT DISTINCT ON (corr.project_did, corr.user_email, corr.start_time,
                        corr.site_name, corr.site_id, corr.task)
        corr.project,
        NULL::integer AS project_number,
        corr.project_did, corr.site_name, corr.site_id,
        corr.task,
        regexp_replace(corr.task, '^\d+\.\s+', '') AS task_clean,
        corr.start_time, corr.corrected_end_time, corr.corrected_duration_min,
        nm.user_name, corr.user_email, nm.user_role,
        '00000000-0000-0000-0000-000000000002'::uuid AS run_id,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS run_date,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS start_date,
        (COALESCE(corr.corrected_end_time, corr.start_time)
            AT TIME ZONE 'America/New_York')::date AS end_date,
        NOW() AS loaded_at
    FROM app_timer.corrections corr
    LEFT JOIN (
        SELECT user_email,
               (array_agg(user_name ORDER BY start_time DESC)
                  FILTER (WHERE user_name IS NOT NULL AND user_name <> ''))[1] AS user_name,
               (array_agg(user_role ORDER BY start_time DESC)
                  FILTER (WHERE user_role IS NOT NULL AND user_role <> ''))[1] AS user_role
        FROM data_staging.stg_timer_activities
        GROUP BY user_email
    ) nm ON nm.user_email = corr.user_email
    WHERE corr.status = 'corrected'
      AND NOT EXISTS (
          SELECT 1 FROM data_staging.stg_timer_activities_clean t
          WHERE t.project_did = corr.project_did
            AND t.user_email  = corr.user_email
            AND t.start_time  = corr.start_time
            AND t.site_name IS NOT DISTINCT FROM corr.site_name
            AND t.site_id   IS NOT DISTINCT FROM corr.site_id
            AND t.task      IS NOT DISTINCT FROM corr.task
            AND t.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND t.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = corr.project_did
            AND rm.user_email  = corr.user_email
            AND rm.start_time  = corr.start_time
            AND rm.site_name IS NOT DISTINCT FROM corr.site_name
            AND rm.site_id   IS NOT DISTINCT FROM corr.site_id
            AND rm.task      IS NOT DISTINCT FROM corr.task
            AND rm.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND rm.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      );

    -- Step 5 (migration 197): drop UNTRACKED same-start runaway duplicates.
    DELETE FROM data_staging.stg_timer_activities_clean cln
    WHERE cln.duration_min > 720
      AND EXISTS (
          SELECT 1 FROM data_staging.stg_timer_activities_clean t2
          WHERE t2.project_did = cln.project_did
            AND t2.user_email  = cln.user_email
            AND t2.start_time  = cln.start_time
            AND t2.site_name IS NOT DISTINCT FROM cln.site_name
            AND t2.site_id   IS NOT DISTINCT FROM cln.site_id
            AND t2.task      IS NOT DISTINCT FROM cln.task
            AND t2.duration_min <= 720
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.corrections c
          WHERE c.project_did = cln.project_did
            AND c.user_email  = cln.user_email
            AND c.start_time  = cln.start_time
            AND c.site_name IS NOT DISTINCT FROM cln.site_name
            AND c.site_id   IS NOT DISTINCT FROM cln.site_id
            AND c.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = cln.project_did
            AND rm.user_email  = cln.user_email
            AND rm.start_time  = cln.start_time
            AND rm.site_name IS NOT DISTINCT FROM cln.site_name
            AND rm.site_id   IS NOT DISTINCT FROM cln.site_id
            AND rm.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.duplicate_reviews r
          WHERE r.project_did = cln.project_did
            AND r.user_email  = cln.user_email
            AND r.start_time  = cln.start_time
            AND r.site_name IS NOT DISTINCT FROM cln.site_name
            AND r.site_id   IS NOT DISTINCT FROM cln.site_id
            AND r.task      IS NOT DISTINCT FROM cln.task
      );
END;
$function$;

-- ---------------------------------------------------------------------------
-- ROLLBACK: re-apply this file's CREATE OR REPLACE FUNCTION with the single
-- DELETE line replaced by:
--     TRUNCATE TABLE data_staging.stg_timer_activities_clean;
-- (the pre-218 live body is exactly that). No other object changed.
-- ---------------------------------------------------------------------------
```

- [ ] **Step 2: Cross-check the transcription.** Diff the function body in the file against the migration-197 chain (`swift_api_pipeline/migrations/197_*.sql` carries the most recent committed body) and confirm the only deltas vs 197 are whatever 197's successors added plus the DELETE line. Note any difference in the report rather than "fixing" the plan's captured body (the live body is authoritative; the controller re-verifies at apply time).
- [ ] **Step 3: Verify 218 is still the next free number** (`ls swift_api_pipeline/migrations | grep -E '^21'`).
- [ ] **Step 4: Commit** `git add swift_api_pipeline/migrations/218_rebuild_timer_clean_nonblocking.sql && git commit -m "feat(migrations): 218 non-blocking rebuild_timer_clean (DELETE, not TRUNCATE)"`

### Task 2: Apply migration 218 + live non-blocking proof (CONTROLLER task, no subagent)

- [ ] **Step 1: Pre-apply drift check** via MCP `execute_sql`: `SELECT position('TRUNCATE TABLE data_staging.stg_timer_activities_clean' IN pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure)) > 0;` must be true. Record `SELECT count(*) FROM data_staging.stg_timer_activities_clean;` as the parity baseline.
- [ ] **Step 2: Apply** via MCP `apply_migration` (name `218_rebuild_timer_clean_nonblocking`) in a quiet window (avoid :21 ET nightly timer run and correction applies; check `gh run list` first).
- [ ] **Step 3: Non-blocking proof.** Kick a rebuild via a long-running MCP `execute_sql` call: `SELECT data_staging.rebuild_timer_clean();` in one call, and WHILE it runs (from a second `execute_sql`) time 5 consecutive `SELECT count(*) FROM data_staging.stg_timer_activities_clean;` reads. Every read must return in under ~2s with the pre-rebuild count (before 218 they would hang until the rebuild finished). If MCP serializes the two calls, fall back to: run the rebuild via `psql`/pooler from Bash in the background and the count loop via MCP.
- [ ] **Step 4: Parity + duration.** After commit: count within ~1% of baseline (new data may have landed); record rebuild duration from `pg_stat_statements` (expect under ~90s); confirm the next `pipeline-timer` or `timer-correction-apply` run stays green (`gh run list`).

### Task 3: `pipeline_health_watcher.py` (TDD on the pure logic)

**Files:**
- Create: `swift_api_pipeline/pipeline_health_watcher.py`
- Test: `swift_api_pipeline/test_pipeline_health_watcher.py`

**Interfaces:**
- Produces: CLI `python pipeline_health_watcher.py [--dry-run] [--force-findings]`; exit 0 = healthy or findings emailed, exit 1 = watcher itself crashed. Pure functions consumed by tests: `evaluate_failed_runs(rows) -> list[str]`, `evaluate_staleness(rows, now, threshold_min=30) -> list[str]`, `evaluate_blanks(counts) -> list[str]`, `evaluate_rebuild_duration(max_ms, threshold_ms=120000) -> list[str]`, `build_email_body(findings, checked_at) -> str`.

- [ ] **Step 1: Write the failing tests** (`pytest` is the repo's runner for `test_*.py`):

```python
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
```

- [ ] **Step 2: Run to verify failure.** `cd swift_api_pipeline && python -m pytest test_pipeline_health_watcher.py -q` -> import error (module missing).
- [ ] **Step 3: Implement.** Full file:

```python
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
# Located by command text (jobids change when jobs are recreated).
WATCHED_JOBS = {
    "dr_task_rollup": "refresh_dr_task_rollup_safe",
    "timer_day_rollup": "refresh_mv_timer_day_rollup",
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
```

- [ ] **Step 4: Run tests.** `python -m pytest test_pipeline_health_watcher.py -q` -> all pass. Also byte-check the repo conventions: `config.get_db/retry_db/close_db` signatures match `roster_gap_watcher.py`'s usage (adapt the call shapes to whatever `config.py` actually exposes and note any adaptation in the report; the pure evaluators must NOT change).
- [ ] **Step 5: Dry-run against the live DB** (env from `swift_api_pipeline/.env` if present locally; skip gracefully and say so in the report if no local DB creds): `python pipeline_health_watcher.py --dry-run`. Expect green or explainable findings.
- [ ] **Step 6: Commit** `git add swift_api_pipeline/pipeline_health_watcher.py swift_api_pipeline/test_pipeline_health_watcher.py && git commit -m "feat(watcher): pipeline health watcher (failed/stale/blank/slow checks, email on findings)"`

### Task 4: GHA workflow

**Files:**
- Create: `.github/workflows/pipeline-health-watch.yml`

- [ ] **Step 1: Write the file** (roster-gap-watcher.yml is the template; watcher needs DB creds + the notifier Gmail token only):

```yaml
name: "Watch: Pipeline Health"

on:
  schedule:
    - cron: '0 18 * * *'   # daily 18:00 UTC = 2:00 PM ET / 2:00 AM PHT
  repository_dispatch:
    types: [pipeline-health-watch]
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (print findings, no email)"
        required: false
        default: "false"

concurrency:
  group: pipeline-health-watch
  cancel-in-progress: false

env:
  PIPELINE_DIR: swift_api_pipeline

jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Create .env
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          cat > .env <<'DOTENV'
          SWIFT_EMAIL=mgmt@ontel.co
          SUPABASE_URL=https://voqfjfngdpcvevbkikud.supabase.co
          SUPABASE_HOST=aws-0-ap-southeast-1.pooler.supabase.com
          SUPABASE_PORT=5432
          SUPABASE_DB=postgres
          SUPABASE_USER=postgres.voqfjfngdpcvevbkikud
          SWIFT_PASSWORD=${{ secrets.SWIFT_PASSWORD }}
          SUPABASE_PASSWORD=${{ secrets.SUPABASE_PASSWORD }}
          DOTENV

      - name: Decode credentials
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          mkdir -p gmail_credentials
          echo "${{ secrets.NOTIFIER_CREDENTIALS_JSON }}" | base64 -d > gmail_credentials/credentials.json
          echo "${{ secrets.NOTIFIER_TOKEN_PICKLE }}" | base64 -d > gmail_credentials/token.pickle

      - name: Watch pipeline health
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          DRY_ARG=""
          if [ "${{ github.event.inputs.dry_run }}" = "true" ]; then
            DRY_ARG="--dry-run"
          fi
          python -u pipeline_health_watcher.py $DRY_ARG
```

- [ ] **Step 2: Sanity-check the secret names** against `.github/workflows/pipeline-timer.yml` (it decodes `NOTIFIER_CREDENTIALS_JSON` -> `credentials.json` and `NOTIFIER_TOKEN_PICKLE` -> `token.pickle`; use exactly the names that file uses). If `gmail_client.authenticate` expects different filenames, match reality and note it.
- [ ] **Step 3: Commit** `git add .github/workflows/pipeline-health-watch.yml && git commit -m "feat(watcher): daily pipeline-health-watch workflow"`

### Task 5: Ship PR A (CONTROLLER task)

- [ ] **Step 1:** Push branch, open PR (title "Non-blocking rebuild_timer_clean (218) + pipeline health watcher"). Body: incident summary, migration 218 applied+verified numbers (from Task 2), watcher checks list, the GHA-cron-instead-of-Apps-Script deviation flagged explicitly.
- [ ] **Step 2:** Run the premerge-review skill lanes appropriate to a pipeline repo (code review on the diff; DB preflight already done in Task 2). Merge.
- [ ] **Step 3:** Post-merge: `gh workflow run pipeline-health-watch.yml -f dry_run=true` on main, confirm green log with "checks green"; then `python pipeline_health_watcher.py --force-findings` FROM A LOCAL SHELL with creds (or a second workflow_dispatch without dry_run right after temporarily... no: use the local shell) to confirm the email arrives at jamil.mendez@ontel.co. Verify the header stamp: add the applied+verified numbers to migration 218's header in the same PR (or a follow-up commit before merge).

# PR B - ontel-people (localhost-gated)

### Task 6: Retry-wrap the getSession lookups

**Files:**
- Modify: `lib/auth/session.ts` (the parallel lookup at lines ~55-72)

**Interfaces:**
- Consumes: existing `withDbRetry` from `@/lib/hr/db-retry` (same helper review-queries uses: `withDbRetry(async () => { ...; if (error) throw ...; return value; })`, retry-once on transient DB failures).

- [ ] **Step 1: Make the edit.** In `lib/auth/session.ts`, add the import `import { withDbRetry } from "@/lib/hr/db-retry";` and wrap the existing `Promise.all` block so a transient failure of the ALLOWLIST read retries once (the name lookup stays best-effort inside the same round trip):

```ts
  // Retry-once on transient DB failures (brownout windows: 2026-07-31 and
  // 2026-08-04 both killed this exact read with instance-wide 503s and threw
  // every page to the error boundary). The allowlist error throws INSIDE the
  // retry closure so withDbRetry can retry it; the misconfiguration message
  // below is preserved on double failure. The directory-name lookup stays
  // best-effort: its error never throws.
  const [allowRes, dirRes] = await withDbRetry(async () =>
    Promise.all([
      svc
        .schema("app_hr")
        .from("hr_app_user")
        .select("id, email, role, is_active, auth_user_id")
        .eq("email", email)
        .eq("is_active", true)
        .maybeSingle<{
          id: number; email: string; role: AppRole; is_active: boolean; auth_user_id: string | null;
        }>()
        .then((res) => {
          if (res.error) {
            throw new Error(
              `app_hr allowlist lookup failed (${res.error.code ?? "?"}): ${res.error.message}. ` +
                `If this is PGRST106, app_hr is not on the PostgREST exposed-schemas list (see migrations/006).`,
            );
          }
          return res;
        }),
      svc
        .schema("app_hr")
        .from("hr_employee_version")
        .select("full_name")
        .is("effective_to", null)
        .ilike("email", email) // email is already normalized; no % or _ to escape
        .limit(1),
    ]),
  );
  const data = allowRes.data;
  const dirRows = dirRes.data;
```

Then DELETE the now-redundant original `const [{ data, error }, { data: dirRows }] = await Promise.all([...])` block and the standalone `if (error) { throw ... }` block (the throw moved inside the closure verbatim). Everything downstream (`if (!data) ...`, link-on-first-sighting, dirName, preview) is untouched.

- [ ] **Step 2: Gates.** `npm run lint && npm run build && npm test` in an ontel-people worktree on a fresh branch `fix/auth-read-retry` cut from origin/main. Expected: all green (no test covers getSession directly; the build's type-check is the guard here).
- [ ] **Step 3: Commit** `git commit -m "fix(auth): retry-once on transient DB failures in the session lookups"`.
- [ ] **Step 4 (CONTROLLER): localhost gate.** Start `npx next dev -p 3100` in the worktree, hand Jamil the URL, get his explicit OK (standing rule; sign-in + one page load is sufficient exercise of this path since getSession runs on every request).
- [ ] **Step 5 (CONTROLLER):** Merge via PR after his OK, deploy-verify, then remove the worktree.

## Self-review notes (applied)

- Spec coverage: Part A -> Tasks 1-2; Part B -> Tasks 3-5; Part C -> Task 6; verification plan -> Tasks 2/5/6 steps. Scheduling deviation from spec (GHA cron now, Apps Script later) is a Global Constraint with an explicit surface-to-Jamil requirement.
- The embedded function body was captured live this session; Task 1 Step 2 and Task 2 Step 1 both re-verify against drift before anything applies.
- Type consistency: evaluator names/signatures identical between test file and implementation; `WATCHED_JOBS` needles match the live refresh function names used by cron (verified in cron.job earlier tonight: jobs run `refresh_dr_task_rollup_safe` via jobid 9's command and `refresh_mv_timer_day_rollup` via the 10-min job).
