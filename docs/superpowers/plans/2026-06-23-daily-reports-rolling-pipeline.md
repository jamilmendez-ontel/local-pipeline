# Daily Reports Rolling Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one rolling pipeline that refreshes a last-30-days window of all daily-reports data (status/approval + hours + timers) every ~10 minutes, replacing the split daily + requirements jobs, with failure-only email and run-tracking. Old two stay live until separately retired.

**Architecture:** Reuse the existing `extract_daily_reports.py` extractor unchanged in behavior — `DailyReportsPipeline.run(projects, full=False, days=N)` with **no** mode flags already fetches all three datasets (tasks at Step 3, requirements at Step 4a, timers at Step 4b). Add a thin wrapper (`run_daily_reports_rolling.py`) that runs it over a 30-day window, records `pipeline.pipeline_runs`, and emails only on failure. A new GHA workflow + a new Apps Script trigger drive it on a 10-min cadence via a dedicated `daily-reports-rolling` dispatch event.

**Tech Stack:** Python 3.12, asyncpg via the project `db`/`config` layer, GitHub Actions, Google Apps Script. Repo `local-pipeline` (public → unlimited Actions minutes).

## Global Constraints

- Python 3.12; install via `pip install -r requirements.txt` from repo root.
- Reuse `extract_daily_reports.py`; do NOT fork the extraction logic.
- Write the SAME three tables with the SAME upsert keys: `data_staging.stg_daily_reports` (key `task_did`), `data_staging.stg_daily_report_hours` (key `task_did, req_id`), `data_staging.stg_daily_report_attendance` (key `task_did, timer_id`). Idempotent — safe to run alongside the legacy two.
- Swift login for this project is `SWIFT_EMAIL=mgmt@ontel.co` (not the main pipeline's login).
- Run-tracking name: `pipeline_name='daily_reports_rolling'`.
- Default window: `--days 30`. Cadence: every 10 min (provisional; revisit from measured `pipeline_runs` durations).
- No success email; failure email only, to `jamil.mendez@ontel.co`.
- Do NOT modify, disable, or remove the legacy `daily-reports.yml`, `run_daily_reports.py`, or the `triggerDaily()` / `triggerRequirements()` Apps Script functions. Retirement is a separate, user-gated step.
- This is ETL wrapper code with no unit-test harness in the repo; validation is by syntax/import checks plus a real `workflow_dispatch` run verified against the DB (see Task 5).

---

### Task 1: Return record counts from `DailyReportsPipeline.run()`

So the wrapper can record `records_extracted`. Purely additive — existing callers (`run_daily_reports.py`) ignore the return value.

**Files:**
- Modify: `swift_api_pipeline/extract_daily_reports.py` (end of `run()`, ~line 462, after the final summary log block)

**Interfaces:**
- Produces: `DailyReportsPipeline.run(...)` now returns `dict[str, int]` with keys `tasks`, `requirements`, `timers` (the staged row counts for this run). Returning a value from a function whose callers ignore it is backward-compatible.

- [ ] **Step 1: Add the return statement**

In `swift_api_pipeline/extract_daily_reports.py`, at the very end of the `run()` method (immediately after the `logger.info(f"{'='*60}")` final summary line, before the method returns implicitly), add:

```python
        return {
            "tasks": len(stg_batch),
            "requirements": len(req_stg_batch),
            "timers": len(tmr_stg_batch),
        }
```

- [ ] **Step 2: Verify it parses and the return is reachable**

Run: `cd swift_api_pipeline && python -c "import ast,sys; ast.parse(open('extract_daily_reports.py').read()); print('OK parse')"`
Expected: `OK parse`

- [ ] **Step 3: Verify existing caller still works (no signature break)**

Run: `cd swift_api_pipeline && python -c "import ast; t=ast.parse(open('run_daily_reports.py').read()); print('OK run_daily_reports parses')"`
Expected: `OK run_daily_reports parses` (confirms the legacy wrapper is untouched and still valid)

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_daily_reports.py
git commit -m "feat(daily-reports): return staged row counts from run() for run-tracking"
```

---

### Task 2: New wrapper `run_daily_reports_rolling.py`

The rolling entry point: runs all three sub-fetches over the window, records `pipeline.pipeline_runs`, fails loud (re-raises → non-zero exit), emails only on failure.

**Files:**
- Create: `swift_api_pipeline/run_daily_reports_rolling.py`

**Interfaces:**
- Consumes: `DailyReportsPipeline.run(projects, full=False, days=N) -> dict` (Task 1); `discover_projects()` from `extract_daily_reports`; `BaseExtractor(pipeline_name).start_pipeline_run(metadata=dict)` and `.complete_pipeline_run(status, records, error)` from `base_extractor`; `run_pipeline_with_notification(func, name, send_email, logger_prefixes, recipients, email_on_success)` from `main`.
- Note: `BaseExtractor(...)` constructor does NOT hit the network (auth is a separate `.authenticate()` call), so creating a tracker instance is cheap and safe. `run_pipeline_with_notification` re-raises on failure after sending the failure email.

- [ ] **Step 1: Create the wrapper**

Create `swift_api_pipeline/run_daily_reports_rolling.py`:

```python
"""Run the Daily Reports ROLLING pipeline.

One job that replaces the split daily + requirements jobs: refreshes a rolling
last-N-days window (default 30) of ALL THREE datasets every run —
  - task status/approval -> data_staging.stg_daily_reports
  - requirements (hours)  -> data_staging.stg_daily_report_hours
  - timers (attendance)   -> data_staging.stg_daily_report_attendance
No success email; failure email only. Runtime is recorded to
pipeline.pipeline_runs as 'daily_reports_rolling'.

Usage:
    python run_daily_reports_rolling.py            # last 30 days
    python run_daily_reports_rolling.py --days 35
"""

import argparse
import sys

from base_extractor import BaseExtractor
from config import get_logger, setup_logging
from main import run_pipeline_with_notification

# Unbuffered output (match the other run_*.py scripts)
sys.stdout.reconfigure(line_buffering=True)

setup_logging()
logger = get_logger("run_daily_reports_rolling")


def run_rolling(days=30):
    """Refresh the rolling window: tasks + requirements + timers for last `days`.

    Calling DailyReportsPipeline.run() with neither timers_only nor
    requirements_only runs Step 4a (requirements) AND Step 4b (timers) plus the
    always-on task load — i.e. all three datasets in one pass.
    """
    from extract_daily_reports import DailyReportsPipeline, discover_projects

    tracker = BaseExtractor(pipeline_name="daily_reports_rolling")
    tracker.start_pipeline_run(metadata={"days": days, "window": "rolling"})
    try:
        logger.info(f"=== ROLLING MODE: all datasets, last {days} days ===")
        projects = discover_projects()
        if not projects:
            logger.info("No active Daily Reports projects found.")
            tracker.complete_pipeline_run("success", records=0)
            return
        pipeline = DailyReportsPipeline()
        counts = pipeline.run(projects, full=False, days=days)
        records = sum((counts or {}).values())
        tracker.complete_pipeline_run("success", records=records)
    except Exception as e:
        tracker.complete_pipeline_run("failed", error=str(e))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Rolling look-back window (default 30)")
    parser.add_argument("--no-email", action="store_true", help="Suppress the failure email too")
    args = parser.parse_args()

    # email_on_success=False -> no email on success; failure email still fires
    # (and re-raises -> non-zero exit -> GHA marks the run failed).
    run_pipeline_with_notification(
        lambda: run_rolling(days=args.days),
        "Daily Reports",
        send_email=not args.no_email,
        logger_prefixes=[
            "pipeline.daily_reports",
            "pipeline.run_daily_reports_rolling",
            "pipeline.base",
            "pipeline.db",
        ],
        recipients=["jamil.mendez@ontel.co"],
        email_on_success=False,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it parses**

Run: `cd swift_api_pipeline && python -c "import ast; ast.parse(open('run_daily_reports_rolling.py').read()); print('OK parse')"`
Expected: `OK parse`

- [ ] **Step 3: Verify argparse wiring (no DB/network needed)**

Run: `cd swift_api_pipeline && python run_daily_reports_rolling.py --help`
Expected: usage text showing `--days` (default 30) and `--no-email`. (`--help` exits before any pipeline/DB work.)

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/run_daily_reports_rolling.py
git commit -m "feat(daily-reports): rolling wrapper (all datasets, N-day window, failure-only email, run-tracked)"
```

---

### Task 3: New workflow `daily-reports-rolling.yml`

Dedicated workflow on its own dispatch event + concurrency group, so the legacy `daily-reports.yml` is untouched.

**Files:**
- Create: `.github/workflows/daily-reports-rolling.yml`

**Interfaces:**
- Consumes: `repository_dispatch` event `daily-reports-rolling` with optional `client_payload.days`; or `workflow_dispatch` with input `days`. Runs `python -u run_daily_reports_rolling.py --days <DAYS>` (Task 2).

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/daily-reports-rolling.yml`:

```yaml
name: "Pipeline: Daily Reports (Rolling)"

on:
  repository_dispatch:
    types: [daily-reports-rolling]
  workflow_dispatch:
    inputs:
      days:
        description: "Rolling look-back window (days)"
        required: false
        default: "30"

concurrency:
  group: daily-reports-rolling
  cancel-in-progress: false

env:
  PIPELINE_DIR: swift_api_pipeline

jobs:
  daily-reports-rolling:
    runs-on: ubuntu-latest
    timeout-minutes: 30

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

      - name: Run rolling daily reports pipeline
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          DAYS="${{ github.event.client_payload.days || github.event.inputs.days || '30' }}"
          python -u run_daily_reports_rolling.py --days "$DAYS"
```

- [ ] **Step 2: Verify YAML is valid**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/daily-reports-rolling.yml')); print('OK yaml')"`
Expected: `OK yaml`

- [ ] **Step 3: Commit and push (workflow_dispatch needs the file on the remote default branch eventually; push the feature branch now)**

```bash
git add .github/workflows/daily-reports-rolling.yml
git commit -m "ci(daily-reports): rolling workflow (10-min cadence target, serialize, 30m timeout)"
```

Note for the executor: `workflow_dispatch` can only be triggered once this workflow file exists on a branch GitHub knows about. Task 5 covers triggering it; if testing from the feature branch, use `gh workflow run daily-reports-rolling.yml --ref feat/daily-reports-rolling-pipeline`.

---

### Task 4: Add `triggerDailyReportsRolling()` to the Apps Script

Add a new trigger function + a rolling-specific dispatch helper. Leave the existing `triggerDaily` / `triggerRequirements` / `fireDispatch` untouched.

**Files:**
- Modify: `scripts/daily_reports_trigger.gs` (append new functions; do not edit existing ones)

**Interfaces:**
- Produces: `triggerDailyReportsRolling()` — fires `repository_dispatch` event `daily-reports-rolling` with `client_payload.days = "30"`. Intended to be installed as an every-10-minutes time-based trigger.

- [ ] **Step 1: Append the new functions**

At the end of `scripts/daily_reports_trigger.gs`, append:

```javascript
/**
 * Rolling trigger — refreshes the last 30 days of ALL daily-reports data
 * (status/approval + hours + timers) in one job. Install as an every-10-min
 * time-based trigger. Replaces the daily + requirements split once that pair
 * is retired (retirement is a separate, manual step — leave triggerDaily /
 * triggerRequirements installed until then).
 */
function triggerDailyReportsRolling() {
  fireRollingDispatch(30);
}

/**
 * Fire repository_dispatch for the rolling workflow (event: daily-reports-rolling).
 */
function fireRollingDispatch(days) {
  var url = "https://api.github.com/repos/" + GITHUB_OWNER + "/" + GITHUB_REPO + "/dispatches";
  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "token " + getToken(),
      "Accept": "application/vnd.github.v3+json"
    },
    payload: JSON.stringify({
      event_type: "daily-reports-rolling",
      client_payload: { days: String(days) }
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  if (code === 204) {
    console.log("✅ Dispatched daily-reports-rolling (days=" + days + ")");
  } else {
    console.log("❌ Rolling dispatch failed: " + code + " " + response.getContentText());
  }
}
```

- [ ] **Step 2: Verify the existing functions are unchanged**

Run: `grep -c "function triggerDaily\b\|function triggerRequirements\|function fireDispatch\b\|function triggerDailyReportsRolling\|function fireRollingDispatch" scripts/daily_reports_trigger.gs`
Expected: `5` (the three originals still present + the two new ones)

- [ ] **Step 3: Commit**

```bash
git add scripts/daily_reports_trigger.gs
git commit -m "feat(daily-reports): Apps Script trigger for rolling 10-min dispatch"
```

**Manual deploy note (for the user, not the executor):** in the Apps Script project, paste the updated file and add a time-based trigger: `triggerDailyReportsRolling` → Time-driven → Minutes timer → Every 10 minutes. Do NOT remove the existing daily/requirements triggers yet.

---

### Task 5: End-to-end validation + cadence measurement

The real test. Trigger one run and verify it does the full job, records timing, and stays silent on success.

**Files:** none (validation only)

- [ ] **Step 1: Push the branch so the workflow is dispatchable**

```bash
git push -u origin feat/daily-reports-rolling-pipeline
```

- [ ] **Step 2: Trigger one rolling run and time it**

```bash
gh workflow run daily-reports-rolling.yml --repo jamilmendez-ontel/local-pipeline --ref feat/daily-reports-rolling-pipeline -f days=30
# wait, then:
RUN=$(gh run list --repo jamilmendez-ontel/local-pipeline --workflow daily-reports-rolling.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN" --repo jamilmendez-ontel/local-pipeline --exit-status
```
Expected: run completes with `success`.

- [ ] **Step 3: Verify the run-tracking row + real duration**

Run this SQL (via Supabase MCP or psql):
```sql
SELECT pipeline_name, status, records_extracted,
       started_at  AT TIME ZONE 'America/New_York' AS started_et,
       completed_at AT TIME ZONE 'America/New_York' AS completed_et,
       round(extract(epoch FROM (completed_at - started_at))/60, 1) AS duration_min
FROM pipeline.pipeline_runs
WHERE pipeline_name = 'daily_reports_rolling'
ORDER BY started_at DESC LIMIT 3;
```
Expected: one `success` row, `records_extracted > 0`, a sane `duration_min` (expected ~3.5-5 min). **This is the measured runtime** that confirms (or revises) the 10-min cadence.

- [ ] **Step 4: Verify all three tables got refreshed by this run**

```sql
SELECT 'reports' t, max(loaded_at AT TIME ZONE 'America/New_York') last_loaded FROM data_staging.stg_daily_reports
UNION ALL SELECT 'hours',  max(loaded_at AT TIME ZONE 'America/New_York') FROM data_staging.stg_daily_report_hours
UNION ALL SELECT 'attend', max(loaded_at AT TIME ZONE 'America/New_York') FROM data_staging.stg_daily_report_attendance;
```
Expected: all three `last_loaded` timestamps are within the run window (today, just now).

- [ ] **Step 5: Confirm no success email fired**

Check the `jamil.mendez@ontel.co` inbox: there should be **no** "Daily Reports" success email from this run. (A failure email would only appear if the run failed.)

- [ ] **Step 6: Record the result**

Append the measured duration to the spec's "Open / provisional" section (or note it in the PR description) and confirm the final cadence with the user. No commit needed unless editing the spec.

---

## Notes for retirement (OUT OF SCOPE — user-gated, do not do as part of this plan)

Once the rolling job has run cleanly for a few days:
1. Remove the `triggerDaily` / `triggerRequirements` time-based triggers in Apps Script.
2. Disable or delete `.github/workflows/daily-reports.yml`.
No DB/downstream change — the rolling job already feeds the same three tables. The legacy period-complete success email retires with the old workflow.
