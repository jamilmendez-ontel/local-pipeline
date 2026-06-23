# Daily Reports Rolling Pipeline — Design

**Date:** 2026-06-23
**Status:** Approved (design); pending implementation plan
**Repo:** `local-pipeline` (public)

## Problem

The "TECH-OPS: Daily Reports" data is currently fed by **two** scheduled jobs that split the work:

- **daily** (every day, ~3-4 AM ET): pulls timers/attendance + re-upserts task status/approval, but only for `work_date` in the **last 3 days**.
- **requirements** (only on the 2nd-5th and 17th-20th): full pay-period sync of hours + descriptions + status/approval, and emails a "period complete" message.

Approvals lag the work date by a median of **11 days** (avg 12.5; **88%** land >3 days out, **16%** land >20 days out). So the daily job's 3-day window almost never captures an approval — approval status is effectively a **bi-monthly** signal, only refreshed by the requirements sweep. A consumer that wants reasonably-fresh approval status can be up to ~2 weeks stale.

## Goal

Replace both jobs with **one** pipeline that refreshes a **rolling last-30-days window every ~10 minutes**, capturing each task's data **before and after** approval. No success email; email **only on failure**. Write the **same** `data_staging` tables so the old two can be retired later with zero downstream change.

The 30-day window covers the bulk of the approval-lag distribution (it misses only the ~16% of approvals that arrive >20 days out; those still get caught while they remain inside the rolling window for 30 days). The **10-minute cadence is provisional** — measured proxy runs put the combined job at ~3.5-5 min; the new job records its own runtime so the cadence can be tuned with real data.

## Scope

In scope:
- A single rolling run that pulls **all three** datasets — tasks (status/approval), requirements (hours/descriptions), timers (attendance) — over a 30-day window.
- Apps Script 10-minute trigger + dedicated GHA workflow + dedicated dispatch event.
- Failure-only email.
- Run-tracking in `pipeline.pipeline_runs`.

Out of scope (explicitly):
- Retiring the old two jobs (separate, user-gated step — see Retirement Path).
- Any change to the three `data_staging` table schemas.
- Backfilling/cleaning the pre-existing future-dated rows (`max(work_date)=2026-12-31`); the extractor already skips `work_date > today`, so the new job won't add more.
- Any HR-app consumption / analytics view (later project).

## Approach (chosen)

**Thin rolling wrapper that reuses `extract_daily_reports.py`.** Reuse the existing, battle-tested extraction functions; add one run path that does all three sub-fetches over a configurable day window with no email. Rejected alternatives: a brand-new standalone extractor (duplicates proven logic, drifts), and repurposing the existing jobs in place (violates the "keep old live until retirement" constraint).

## Components

### 1. `extract_daily_reports.py` (minimal change)
The "all" behavior **already exists**: `DailyReportsPipeline.run(projects, full=False, days=N)` with neither `timers_only` nor `requirements_only` fetches the task list once (Step 3), then runs **both** the requirements sub-fetch (Step 4a) and the timers sub-fetch (Step 4b), with the existing date-window filter (`work_date >= today - N days`; future-dated and `milestones` tasks skipped). So no new "mode" is needed. The only change is to have `run()` **return the staged row counts** (`{tasks, requirements, timers}`) so the wrapper can record `records_extracted`; this is additive (existing callers ignore the return).

### 2. `run_daily_reports_rolling.py` (new entry point)
- Calls the extractor's "all" path with `days=30` (configurable via arg).
- No period detection, no "period complete" email.
- Wraps the run in `pipeline.pipeline_runs` tracking (see #5).
- On exception: record `status='failed'` + error, send the failure email (#4), exit non-zero.

(Implementation detail for the plan: this may be a new flag `--rolling` on the existing `run_daily_reports.py` rather than a separate file, whichever keeps the wrapper smallest. Either way the behavior above is the contract.)

### 3. `.github/workflows/daily-reports-rolling.yml` (new)
- Triggers: `repository_dispatch: daily-reports-rolling` (from Apps Script) + `workflow_dispatch` (manual, with optional `days` input defaulting to 30).
- Same runner/env/secrets setup as `daily-reports.yml` (Supavisor pooler host, `postgres.voqfjfngdpcvevbkikud`, `SWIFT_PASSWORD`, `SUPABASE_PASSWORD`, notifier creds).
- Runs `python run_daily_reports_rolling.py --days 30` (or `--rolling --days 30`).
- **Concurrency:** `group: daily-reports-rolling`, `cancel-in-progress: false` — runs serialize (at most one running + one queued) if a run ever exceeds the interval.
- Timeout: 30 min (well above the ~5-min expected; lower than the legacy 60).

### 4. `scripts/daily_reports_trigger.gs` (add function)
Add `triggerDailyReportsRolling()` that POSTs `repository_dispatch` with event `daily-reports-rolling`. Install a time-based trigger firing **every 10 minutes**. The existing `triggerDaily()` / `triggerRequirements()` stay in place (old jobs keep running).

### 5. Run-tracking — `pipeline.pipeline_runs`
The legacy daily-reports jobs do **not** log here (which is why GitHub Actions history was the only duration source). The new job logs `pipeline_name='daily_reports_rolling'` with `started_at`, `completed_at`, `status`, `records_extracted`. This gives the exact combined runtime so the cadence can be tuned.

### 6. Failure-only email
No email on success. On failure, send one alert email via the existing `pipeline_notifier` to **jamil.mendez@ontel.co** (mirrors the failure-only pattern adopted for `user_priorities` this session). The legacy "period complete" success email is **not** carried over.

## Data flow

```
Apps Script (every 10 min)
  -> repository_dispatch: daily-reports-rolling
    -> GHA daily-reports-rolling.yml
      -> run_daily_reports_rolling.py --days 30
        -> extract_daily_reports.py (all path, last 30 days)
           - tasks      -> data_staging.stg_daily_reports          (upsert key: task_did)
           - requirements -> data_staging.stg_daily_report_hours    (upsert key: task_did, req_id)
           - timers     -> data_staging.stg_daily_report_attendance (upsert key: task_did, timer_id)
        -> pipeline.pipeline_runs (daily_reports_rolling)
   on failure: pipeline_notifier -> jamil.mendez@ontel.co
```

Upserts are idempotent, so the rolling job is safe to run **alongside** the legacy two during the transition.

## Error handling

- Per-run failure -> `pipeline_runs.status='failed'` + `error_message`, failure email, non-zero exit (run shows red in GHA).
- Overlap -> prevented by the concurrency group.
- Transient pool/connect blips (seen ~7/60 in legacy history) -> a failed run simply red-flags; the **next** run 10 min later self-heals (rolling window re-covers the same range). No retry logic needed initially.

## Testing / validation

- Manual `workflow_dispatch` run; confirm all three tables get fresh `loaded_at` and a `pipeline_runs` row with a sane duration.
- Confirm no success email fires; force a failure (e.g. bad cred in a throwaway run) and confirm exactly one failure email.
- Compare row deltas against a legacy daily + requirements run to confirm parity of coverage.
- Observe a few live 10-min cycles; read `pipeline_runs` durations; decide final cadence.

## Retirement path (later, user-gated — NOT part of this build)

Once the rolling job is proven over a few days:
1. Remove `triggerDaily()` / `triggerRequirements()` time triggers from Apps Script.
2. Disable/delete `.github/workflows/daily-reports.yml`.
No DB or downstream change — the rolling job already feeds the same three tables. The legacy period-complete email disappears with the old workflow.

## Open / provisional

- **Cadence** = 10 min, provisional pending `pipeline_runs` durations from the live job.
- **Window** = 30 days. Could widen to 35 for margin past the 16% of approvals landing >20 days out; starting at 30.
