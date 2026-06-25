# Asset-Tasks: Honest Success + Persistent Retry + Row-Count Guard

**Date:** 2026-06-24
**Status:** Approved (design)
**Area:** `swift_api_pipeline` (asset_tasks extraction + notification)

## Problem

On 2026-06-24 the nightly asset-tasks run (GHA run 28074835045) suffered a
~30-min Swift API `503 Service Unavailable` outage. Six of seven TECH-OPS
projects extracted; **TS19 failed all retries** and retained its old data. The
run was recorded `status='success'` (with an `error=` note) and **a SUCCESS
email went out** even though a project failed. The downstream export job
correctly aborted via its guard, but the operator was told "success".

Three gaps:

1. **False success email.** The notification wrapper sends `SUCCESS` whenever the
   pipeline function returns without raising. Partial failures deliberately do
   not raise (so the 6 good projects still transform and downstream's
   `WHERE status='success'` lookup still works), so the email cannot tell partial
   from clean.
2. **Retry not persistent enough.** Only one project-level retry round exists
   (`RETRY_WAIT_SECONDS=300`, 10 attempts). A ~30-min outage outlasts it.
3. **No row-count regression detection.** A project (or the total) silently
   returning 0 or far fewer rows than the previous run is not flagged.

## Goal

A SUCCESS / extract email fires **only** on a genuinely clean run. Partial
failures retry harder, then send a red email if still incomplete. A row-count
regression is caught and surfaced. The successfully-extracted projects always
still flow downstream.

## Current behavior (verified)

- `main.py::run_pipeline_with_notification(func, name, ...)` — binary:
  `func()` returns → builds `PipelineResult(status="SUCCESS")`, sends SUCCESS
  email (gated by `email_on_success`); `func()` raises → `FAILED` email +
  re-raise. **The return value of `func()` is currently ignored.**
- `extract_asset_tasks.py::run_asset_task_pipeline` — on partial failure after
  the single retry pass, calls `complete_pipeline_run("success", total,
  error=error_detail)` and **does not raise**, so transforms run on partial data.
- Existing retry: `RETRY_WAIT_SECONDS = 300`; one pass; failed projects retried
  in parallel (`ThreadPoolExecutor`), 10 attempts each; recovered projects folded
  into success.
- `pipeline.pipeline_runs` columns: `run_id, pipeline_name, status, started_at,
  completed_at, records_extracted, error_message, metadata (jsonb)`.
  `metadata` is written at `start_pipeline_run`; `complete_pipeline_run` does not
  touch it today.
- The `pipeline-asset-tasks-export` workflow has a guard that refuses to export
  when the latest `asset_tasks_extract` run carries an `error=` note. **This stays
  as-is and continues to block downstream on partial/abnormal runs.**

## Design

### Component 1 — Three-way notification outcome
**Files:** `main.py`, `pipeline_notifier.py`

Introduce a small return contract so a pipeline function can report a non-clean
outcome **without raising**:

- New lightweight type in `pipeline_notifier.py`:
  `PipelineOutcome(status, failed_projects=[], abnormal_projects=[], detail="")`
  where `status` ∈ `{"SUCCESS", "PARTIAL", "ABNORMAL"}`.
- `run_pipeline_with_notification` interprets `func()`'s return value:
  - returns `None` / `True` (every other pipeline, unchanged) → **SUCCESS** email
    (still gated by `email_on_success`).
  - returns a `PipelineOutcome` with `status != "SUCCESS"` → **red degraded
    email**, sent whenever `send_email` is true **regardless of
    `email_on_success`** (a degraded run is never routine noise), and the function
    **returns normally (process exits 0)** so transforms / analytics / downstream
    dispatch still run for the good projects.
  - raises → **FAILED** email + re-raise (exit non-zero), as today.

This is the guarantee: **SUCCESS is impossible unless the run is truly clean**,
because the extract function returns a non-`SUCCESS` `PipelineOutcome` whenever a
project failed or a count is abnormal. Applies to both the full `asset_tasks`
run and the `asset_tasks_extract`-only stage (both route through this wrapper).

### Component 2 — Persistent per-project retry
**File:** `extract_asset_tasks.py`

Wrap the existing single retry pass in a loop of **up to 3 rounds, 5-min rest
between rounds**, breaking early once no projects remain failed:

```
MAX_RETRY_ROUNDS = 3          # new
RETRY_WAIT_SECONDS = 300      # existing (rest between rounds)
for round_no in range(1, MAX_RETRY_ROUNDS + 1):
    if not failed_projects: break
    sleep(RETRY_WAIT_SECONDS)
    <existing retry-pass body, operating on current failed_projects>
    failed_projects = still_failed_this_round
```

Adds at most ~15 min, safely under the 90-min job timeout. Recovered projects
fold back into the success set exactly as the current retry-succeeded path does.

### Component 3 — Row-count abnormality guard
**File:** `extract_asset_tasks.py` (+ baseline persisted via `base_extractor.py`)

After extraction (and after cleanup), for each **successfully-extracted**
project compare its fresh count against the previous successful run, plus the
grand total:

- **0 rows** → always abnormal.
- **drop > `ABNORMAL_DROP_PCT` (10%)** vs previous → abnormal. Constant, tunable.
- **No previous baseline** (first run / no prior success) → skip, no false alarm.
- **Failed projects are excluded** from this check (they are already `PARTIAL`,
  and retained old data — not an "abnormal count").

Baseline storage (no migration):
- Extend `complete_pipeline_run(status, records, error, project_counts=None)` to
  merge `{"project_counts": {...}}` into the existing `pipeline_runs.metadata`
  jsonb on every completing run.
- The guard reads the latest prior `status='success'` run for this
  `pipeline_name`: per-project from `metadata->'project_counts'`, grand total
  from `records_extracted`.

### Component 4 — Outcome precedence + email rendering
**File:** `pipeline_notifier.py`

- Precedence: any failed project → `PARTIAL`; else any abnormal → `ABNORMAL`;
  else `SUCCESS`. A run may carry both lists (subject reflects `PARTIAL`, body
  also lists abnormal projects).
- `send_pipeline_email` / the HTML builder render `PARTIAL` and `ABNORMAL` in the
  same red treatment as `FAILED`, plus a section naming offending project(s):
  failed projects, and abnormal projects with `old → new` counts and % change.
- Subjects:
  - `Pipeline PARTIAL FAILURE: Asset Tasks (<duration>)`
  - `Pipeline ABNORMAL ROW COUNT: Asset Tasks (<duration>)`

### Component 5 — Outcome propagation
**File:** `main.py`

`run_asset_tasks_pipeline` (the `func` passed to the wrapper) calls extract →
captures the extract's `PipelineOutcome` → runs transforms (always, for the good
data) → returns the outcome up to `run_pipeline_with_notification`. The
extract-only stage (`run_asset_tasks_extract_pipeline`) returns the extract
outcome directly.

### Downstream interaction (unchanged)
- DB `status` stays `'success'` on partial/abnormal so the good projects'
  transforms resolve via `WHERE status='success'`. Honesty lives in the **email +
  the `error_message` note**, not in flipping the enum (which would strand good
  data).
- The export guard already blocks on the `error=` note; partial/abnormal runs
  keep writing it, so export stays correctly blocked until a clean run.

## Files touched

| File | Change |
|------|--------|
| `swift_api_pipeline/extract_asset_tasks.py` | retry loop (3 rounds); abnormality guard; return `PipelineOutcome` |
| `swift_api_pipeline/main.py` | three-way `run_pipeline_with_notification`; propagate outcome through `run_asset_tasks_pipeline` / extract-only |
| `swift_api_pipeline/pipeline_notifier.py` | `PipelineOutcome` type; red `PARTIAL`/`ABNORMAL` rendering + offending-project section |
| `swift_api_pipeline/base_extractor.py` | `complete_pipeline_run` persists `project_counts` into `metadata` jsonb |

## Constants

- `MAX_RETRY_ROUNDS = 3`
- `RETRY_WAIT_SECONDS = 300` (existing)
- `ABNORMAL_DROP_PCT = 0.10`

## Testing

Unit tests with stubbed DB + notifier:

1. Clean run → `SUCCESS` email, exit 0.
2. One project fails all 3 rounds → `PARTIAL` red email, exit 0, other projects'
   transforms still run, `error=` note written (export stays blocked).
3. Project recovers in round 2 → `SUCCESS` (no degraded email).
4. A project returns 0 rows → `ABNORMAL` red email.
5. A project returns 15% fewer rows than baseline → `ABNORMAL`; a 5% dip → clean.
6. First run / no baseline → guard skipped, no false alarm.
7. Both a failed project and an abnormal project → `PARTIAL` subject, body lists
   both.
8. Regression: an unrelated pipeline whose `func` returns `None` still emails
   `SUCCESS` (backward compatibility).

## Non-goals

- No change to the export guard, the DB status enum, or the per-project
  clear/cleanup logic.
- No new table or schema migration (reuses `pipeline_runs.metadata`).
