# Non-Blocking Timer-Clean Rebuild + Pipeline Health Watcher + Auth-Read Hardening

**Date:** 2026-08-04
**Status:** Approved design, pending implementation plan
**Scope:** local-pipeline (migration 218 + watcher script + GHA workflow) and one small ontel-people PR (auth-read retry, localhost-gated)
**Trigger:** 2026-08-04 ~00:26 ET outage: `data_staging.rebuild_timer_clean()` ran 138.8s inside the nightly pipeline-timer workflow, its TRUNCATE held AccessExclusiveLock on `stg_timer_activities_clean` for the whole transaction, queued readers exhausted the PostgREST pool, and the instance returned 503 on every endpoint (~40s), including auth lookups. Jamil hit the app error boundary. Second incident of the write-window brownout class (first: 2026-07-31).

## 1. Facts established from the live system

- `rebuild_timer_clean()` (last defined in migration 197's chain; live body captured 2026-08-04) does: TRUNCATE -> INSERT DISTINCT ON with three NOT EXISTS anti-joins (duplicate_reviews via jsonb_array_elements, entry_removals, corrections) -> corrections UPDATE -> additions INSERT -> corrections INSERT -> runaway-duplicate DELETE. `SET statement_timeout '300s'`. One transaction.
- Callers that matter: `timer_correction_review.py` `rebuild_clean_table(db)` (lines ~1718/2455), which runs inside BOTH the nightly `pipeline-timer` GHA (daily ~00:21 ET; last night's run 04:21:26Z produced the outage) and the ad-hoc `timer-correction-apply` GHA (3 dispatches on 2026-08-03). The stale-not-blank guard (refuse rebuild when `stg_timer_activities` is empty) lives in `rebuild_clean_table`, NOT in the SQL function.
- Table size: 385,675 rows / 179 MB total relation. Rebuild mean 41.1s (pg_stat_statements); the 138.8s run was an outlier under concurrent load.
- `mv_timer_day_rollup` has a hard pg_depend dependency on `stg_timer_activities_clean`, so a DROP-and-rename table swap is not available without touching the MV. This rules out the rename-swap pattern for the TABLE (it worked for MVs in migration 217 because we rebuilt the MVs themselves).
- Alert channel decision (Jamil): email via the existing pipeline Gmail notifier. Auth-read hardening: included, as a separate ontel-people PR through the localhost gate.

## 2. Part A: non-blocking rebuild (migration 218)

**Change `TRUNCATE TABLE data_staging.stg_timer_activities_clean;` to `DELETE FROM data_staging.stg_timer_activities_clean;` in `rebuild_timer_clean()`. Everything else in the function body stays byte-identical.**

Why this works: TRUNCATE takes AccessExclusiveLock (blocks even SELECT) for the remainder of the transaction. DELETE takes RowExclusiveLock, which does not conflict with readers; under MVCC every concurrent reader sees the complete pre-rebuild table until the transaction commits, then atomically sees the new one. The reader-blocking window drops from minutes to zero. Readers never see a partially rebuilt table in either design (single transaction).

Costs, accepted:
- Rebuild runs somewhat slower (DELETE writes WAL per row; TRUNCATE is O(1)). At 386k rows this is seconds, inside the 300s timeout with wide margin. The watcher's duration alarm (Part B check 4) guards the margin.
- Heap/index bloat: ~179 MB of dead tuples per rebuild cycle, reclaimed by autovacuum; steady state roughly 2x heap. Acceptable at this size.

Explicitly rejected for now (documented as the upgrade path if the table grows ~10x): blue/green physical tables behind a stable-name view with a CREATE OR REPLACE VIEW flip. Zero bloat and sub-ms cutover, but adds view grants, PostgREST exposure of a view, and an MV re-point; not worth it at 179 MB.

Migration 218 contents:
1. `CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()` with the live body, TRUNCATE line replaced by DELETE, plus a header comment naming the incident and the MVCC rationale. The live body MUST be captured from `pg_get_functiondef` at implementation time and diffed against the migration-197-era body to rule out drift (the DB-preflight habit); the function is not byte-tracked by any single migration file today.
2. A preflight DO block asserting the live function still contains the `TRUNCATE TABLE data_staging.stg_timer_activities_clean` line (aborts if already applied or drifted).
3. No table, view, or grant changes. Rollback: re-apply the captured pre-218 body (stored verbatim in the migration's rollback footer).

Non-goal: making the rebuild incremental. The DISTINCT ON + anti-join semantics stay untouched.

## 3. Part B: pipeline health watcher (local-pipeline)

New `swift_api_pipeline/pipeline_health_watcher.py`, modeled on `roster_gap_watcher.py` (read-only DB probes, plain-text findings, exit code drives GHA step status). Runs daily via a new GHA workflow `pipeline-health-watch.yml` (`repository_dispatch` type `pipeline-health-watch` + `workflow_dispatch` for manual runs), scheduled from Apps Script per the standing scheduling rule (GHA cron is unreliable). Sends email ONLY when at least one finding fires, via the existing pipeline Gmail notifier (`pipeline_notifier.py` account; recipients: jamil.mendez@ontel.co per the email-recipients rule).

Checks (each finding carries exact numbers and the query it came from):
1. **Failed cron runs, last 24h.** `cron.job_run_details` WHERE status <> 'succeeded', ordered by `runid DESC` (failed rows can carry NULL start_time; never order by start_time). Reports jobid, command head, count, most recent return_message.
2. **Silent-stale refreshes.** For the 5-min DR chain job (command matches `refresh_mv_daily_report_task_rollup` / `refresh_dr_task_rollup_safe`) and the 10-min rollup job (`refresh_mv_timer_day_rollup` / `refresh_timer_day_rollup_safe`): last SUCCESSFUL run older than 30 minutes at check time. Jobs are located by command text, not hardcoded jobid (jobids can change if jobs are recreated).
3. **Blank-source guard.** Row count zero on any of: `data_staging.stg_timer_activities_clean`, `analytics.mv_timer_day_rollup`, `analytics.mv_hr_report_review`. Zero here means the stale-not-blank guards were bypassed or a rebuild ran from an empty source; this is the silent worst case for DR Monitoring / Hours Variance.
4. **Rebuild duration headroom.** `pg_stat_statements` max_exec_time for the `rebuild_timer_clean` statement above 120,000 ms (40% of its 300s timeout). Early warning that the outlier is becoming the norm.

Behavior: healthy run = no email, GHA step green, one summary line in the job log. Any finding = one email listing all findings. Watcher self-death is covered by GitHub's own workflow-failure notifications (the script exits non-zero on unexpected exceptions).

Explicit non-goals: no auto-remediation (no restarts, no refresh calls), no Chat webhook (email decision), no sub-daily cadence for now (the deferred jobid-9 watcher item asked for detection, not real-time paging; cadence can tighten later by adding another Apps Script trigger).

## 4. Part C: auth-read hardening (ontel-people, separate PR)

Wrap the two per-request auth-path reads that produced the error page in the existing `withDbRetry` helper (retry-once on transient failures; reads only, retry-safe):
- the `hr_app_user` active-user lookup (role gate), and
- the `hr_employee_version` display-name lookup,
at their query sites (exact files located at plan time; both surfaced in the outage logs as the 503s that killed the page render).

Constraints: no behavior change on success paths; on double failure the existing error path stays (error boundary with auto-retry). Gates + Jamil's localhost verification at :3100 BEFORE merge (standing rule).

## 5. Verification plan

- Migration 218: apply via MCP `apply_migration` in a quiet window. Then prove non-blocking directly: start `SELECT data_staging.rebuild_timer_clean()` in one connection while a loop of `SELECT count(*) FROM data_staging.stg_timer_activities_clean` reads run in another; assert reads return in milliseconds throughout (before 218 they would block). Record rebuild duration before/after; confirm row-count parity with the pre-218 rebuild.
- Watcher: run locally with a deliberately low threshold (e.g. staleness 0 minutes) to force findings and confirm the email arrives at jamil.mendez@ontel.co with correct numbers; then run with real thresholds and confirm silent-green. Confirm the GHA workflow runs via `workflow_dispatch`; Apps Script trigger added alongside the existing dispatch triggers (dispatch PAT gotcha: the PAT and its GHA secret must be the ones already in use; no rotation here).
- ontel-people PR: unit gates + localhost:3100 verification by Jamil, then merge and deploy-verify.

## 6. Deferred / named non-goals

- Blue/green view swap for `stg_timer_activities_clean` (upgrade path if the table grows ~10x).
- Sub-daily watcher cadence and Chat alerts.
- Incremental (non-full) timer-clean rebuild.
- Why the 2026-08-04 run took 138.8s vs the 41s mean (likely concurrent load; revisit only if the duration alarm fires).
