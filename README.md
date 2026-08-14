# Local Pipeline — Swift API → Supabase ETL

ETL pipeline that extracts data from the Swift Projects API, Gmail, and
Google Calendar/Sheets, transforms it into clean staging tables, and
maintains analytics views in Supabase (PostgreSQL).

```
Swift API ───┐
Gmail ───────┼──► data_raw (JSONB) ──► data_staging (normalized) ──► analytics (views/MVs)
Calendar ────┤
Sheets ──────┘
```

## Database schemas

| Schema | Purpose |
|--------|---------|
| `data_raw` | Raw API responses (JSONB) for the heavy pipelines |
| `data_staging` | Cleaned, normalized staging tables (asyncpg COPY-loaded) |
| `analytics` | Pre-joined views + materialized views for downstream reports |
| `reference` | Manually-maintained lookup tables (`report_targets`, `report_group_meta`, etc.) |
| `pipeline` | Run-tracking metadata (`pipeline_runs`) |
| `agent` | DARA assistant's `schema_metadata` table |

## Automation — GitHub Actions only (Windows Task Scheduler retired 2026-05-28)

All nightly pipelines run as GHA workflows in this repo, fired by Apps
Script time-driven triggers under the notifier account. See
`scripts/pipeline_trigger.gs` for the full schedule.

| Workflow | Trigger | What it refreshes |
|---|---|---|
| `pipeline-orgs.yml` | Nightly Apps Script | Orgs + projects (Phase 1, must run first) |
| `pipeline-timer.yml` | Nightly Apps Script | `stg_timer_activities` + `stg_timer_corrections` apply + clean rebuild |
| `pipeline-priorities.yml` | Nightly Apps Script | `stg_user_priorities` |
| `pipeline-forms.yml` | Nightly Apps Script | `stg_qa_form`. Before extraction, auto-discovers new TS projects' QA forms via the Swift REST API and registers them (see below) |
| `pipeline-timer-discrepancies.yml` | Nightly Apps Script | Google Form → `stg_timer_discrepancies` |
| `pipeline-calendar-events.yml` | Apps Script (5 AM & 5 PM ET) | Google Calendar → `stg_calendar_events` (incremental, AI-normalized; per-kind `analytics.v_calendar_*` views). Self-heals on failure: 4 in-job attempts with backoff, plus the auto-rerun workflow below |
| `pipeline-calendar-events-autorerun.yml` | `workflow_run` on calendar-events failure | Safety net: waits 10 min then re-runs the failed calendar-events jobs, up to 2 automatic reruns (`run_attempt < 3`), so a failed run recovers in ~20-30 min instead of waiting ~12h for the next dispatch |
| `pipeline-asset-tasks.yml` | Nightly Apps Script | Heavy nightly: asset_tasks → MVs → fires downstream dispatches |
| `pipeline-asset-tasks-inc.yml` | Hourly GHA cron :20 (pilot); UTC-hour-05/06 run = strict audit gate | SHADOW: incremental asset-tasks walker → `*_inc` tables + drift audit (see below) |
| `pipeline-asset-tasks-gc.yml` | Nightly Apps Script | Parallel GC pipeline (non-Ontel orgs) |
| `pipeline-open-items-data.yml` | Nightly Apps Script | OIR-scoped Swift snapshots + downstream report dispatch |
| `gmail-pipeline.yml` | Apps Script gmail_trigger.gs (frequent) | AR aging + sales detail when Daily Revenue Report email arrives |
| `timer-correction-apply.yml` | Apps Script onFormSubmit | Apply timer-duration corrections in real time. Responses whose entry-hash went stale (the entry changed after the review email — running timer completed, re-extract drift) are resolved via the form's Entry Details prefill to the entry's start-key group instead of being silently skipped; ambiguous matches still skip with a warning (2026-07-21) |
| `timer-duplicate-resolve.yml` | Apps Script onFormSubmit | Resolve timer-duplicate reviews in real time |
| `pipeline-health-watch.yml` | Daily 18:00 UTC | Warehouse health checks (failed cron runs, stale refreshes, blank tables, slow rebuild; the rebuild alarm auto-resets its pg_stat high-water after firing so one outlier can't re-email daily); emails Jamil only on findings, silent when healthy |

Exact trigger times live in `scripts/pipeline_trigger.gs`. The
`pipeline-asset-tasks` workflow fires downstream dispatches at end-of-run
(export, discrepancies, validator, weekly compliance). The
`pipeline-open-items-data` workflow fires a downstream report email
dispatch based on day-of-week.

Cross-repo dispatches use a dispatch PAT stored in GHA secrets.

## Pipeline architecture (`swift_api_pipeline/`)

```
main.py
├── Phase 1: Organizations & Projects
│   └── pipeline.py + transform.py → stg_organizations, stg_projects
│
├── Phase 2: Parallel (ThreadPoolExecutor)
│   ├── extract_asset_tasks.py → raw_asset_tasks (6 workers, COPY)
│   │   └── transform.py → stg_assets (RPC), stg_asset_tasks (server-side SQL)
│   ├── pipeline.py:user_priorities → raw_user_priorities → stg_user_priorities
│   ├── extract_forms.py → raw_form_qa_ts13..ts19 (dynamic, `reference.ref_qa_forms`) → stg_qa_form
│   └── extract_timer.py → raw_timer_activities → stg_timer_activities
│       └── data_staging.rebuild_timer_clean() → stg_timer_activities_clean
│
└── Post-Phase 2:
    ├── data_staging.backfill_asset_did() (3-pass: asset_id → asset_name → FA regex)
    └── analytics.refresh_one_mv() × 5 (mv_project_summary, mv_technician_stats, mv_daily_completion,
        mv_timer_revenue — timer→revenue attribution, migrations 209-212: asset-path market crosswalk
        + rate-sheet amounts split by tech time-share; see docs/specs/timer-revenue-market-crosswalk.md,
        mv_timer_revenue_daily — per-day proration for the ontel-people revenue embed, migrations 214,
        225 and 228; listed after mv_timer_revenue because it reads from it)
```

**Step order across the two nightly workflows is load-bearing for `asset_did`.**
`pipeline-asset-tasks` runs at 04:18 UTC and `pipeline-timer` after it (04:21 for
years; dispatched ~05:15 UTC / 1:15 AM ET since the 2026-08-06 trigger move), and
`transform_timer_activities` does `DELETE FROM stg_timer_activities WHERE
start_date = <extraction MONTH bucket>` then re-inserts **without** `asset_did`.
So asset-tasks' backfill was always undone three minutes later, and
`rebuild_timer_clean()` then copied the NULLs into the clean table: `asset_did`
was structurally NULL for the whole current month-to-date and only filled in
once the month rolled over and the DELETE stopped touching those rows (measured
2026-08-07: 0 of 2,354 rows in the 2026-08-01 bucket, against 77-80% for every
earlier month). No asset means no market means no rate, so attributed revenue was
blank for the current month, every month.

`pipeline-timer` therefore runs its own **backfill after the reload** and its own
**analytics refresh after `rebuild_timer_clean()`** (the 04:18 refresh cannot see
timer data that lands at 04:21). Do not remove either step, and do not rely on
asset-tasks' backfill for timer rows. The durable fix is to populate `asset_did`
during the transform's INSERT so no ordering dependency exists at all; until then
these two steps are what keep the current month priced.

### Incremental asset-tasks shadow (pilot, 2026-07)

`extract_asset_tasks_inc.py` is a SHADOW duplicate of the asset-tasks
pipeline that walks the project → asset → task hierarchy pruned by
`lastUpdated`, fetching and writing only what changed (guarded upserts +
keep-list reconcile; no run_id sweeps). It writes ONLY to
`raw_asset_tasks_inc` / `stg_assets_inc` / `stg_asset_tasks_inc` and
namespaced `pipeline.content_watermarks` rows — **the current
`pipeline-asset-tasks.yml` remains authoritative** until the pilot exits.
Pilot scope is TS17–19 (`--projects all13` widens to phase 2). Measured
2026-07-10: baseline seed 46.9 min, incremental re-run 48 s (~58×).

- Audit: `python audit_asset_tasks_inc.py` diffs `stg_asset_tasks_inc`
  against `stg_asset_tasks` per project and persists every result to
  `pipeline.inc_audit_results`; the workflow fails on mismatch. Run-timing
  churn self-corrects by the next audit; persistent drift is a bug.
- Force resync: `python extract_asset_tasks_inc.py --baseline`, or
  `DELETE FROM pipeline.content_watermarks WHERE pipeline_name LIKE
  'asset_tasks_inc/%'` and run incrementally.
- Pilot exit gates (decided by Jamil, not the code): phase 1 → widen to
  all TS13+ after 7 days of every-2h runs with zero unexplained audit
  mismatches and delete-propagation covered (natively or by the Sunday
  `--full-walk`); phase 2 → restructure the current pipeline on this
  pattern (and gc-asset-tasks after it) after 7+ clean days at full scope
  with runtime/IO a small fraction of the full reload's.

### Dynamic TS project coverage + QA form auto-discovery (2026-08-11)

The three nightly Excel export scripts (`scripts-reference/export_asset_tasks_excel.py`,
`export_timer_excel.py`, `export_qa_form_excel.py`, fired by `pipeline-asset-tasks-export.yml`
/ `pipeline-timer.yml` / `pipeline-forms.yml`) read their TS project list from
`reference.ref_ontel_techops_projects` (`ts_projects.py`) instead of a hardcoded TS13–TS19
list, so a new TS project is covered the moment it lands in `stg_projects`, no code change
needed. A brand-new TS with no rows yet is printed as `SKIPPED (new/empty)` and left out of
that night's workbook rather than failing the export guard.

`config.py`'s `QA_FORMS` dict has been replaced by `reference.ref_qa_forms` (migration 231,
RLS on, seeded with TS13–TS19). `pipeline-forms.yml` discovers a new TS's QA form
automatically before extraction: it calls `GET /api/organizations/{org}/forms` and matches
the title `ACTIVE - QA Form TS{n}`. Exactly one match registers the form (inserts the
`ref_qa_forms` row, creates `raw_form_qa_ts{n}` from a version-controlled DDL template) and
emails jamil.mendez@ontel.co a veto-framed confirmation; zero matches retry quietly every
night (with a 7-day escalation email once the TS has asset-task rows but still no form);
multiple matches send an alert asking for a manual pick. Discovery failures degrade to an
alert email and never block that night's extraction of the already-registered forms. See
`docs/superpowers/specs/2026-08-11-ts-project-auto-coverage-design.md` for the full design.

### Targeted extractors (report-driven)

Lightweight pipelines that snapshot Swift data for specific
`(org, project)` sets defined in `reference.report_targets`. Used by
report-automation reports that don't need full-scale extracts.

| Extractor | Output | Used by |
|---|---|---|
| `extract_targeted_asset_tasks.py` | `data_staging.stg_targeted_asset_tasks` | Open Items Report (Final COP date enrichment) |
| `extract_targeted_task_requirements.py` | `data_staging.stg_targeted_task_requirements` | Open Items Report (per-requirement detail) |

`stg_targeted_asset_tasks` captures both `task_approved_on` and
`task_submitted_on` from the upstream API's epoch fields.

### Swift schedule feed audit (2026-08-14)

`schedule_feed_audit.py` cross-checks every scheduled task in the User
Priorities report against the task's own activity feed (Firebase RTDB).
Swift's server-side calendar scheduling path stores **timed schedules 12
hours early** on the task record (its date-only noon→midnight normalization
applied unconditionally) while the activity feed keeps the correct instant —
so the feed wins whenever the two disagree.

- Registry: `pipeline.schedule_audit_anomalies` (+ `schedule_audit_runs`),
  migrations 236-237. Classes: `timed_mismatch`, `ghost_schedule`
  (feed says removed, task still scheduled), `no_feed_schedule`.
  Date-only midnight-ET (task) vs noon-ET (feed) is benign and excluded.
- Serving: `analytics.v_user_priorities_effective` overlays
  `scheduled_effective` (feed value) while an anomaly is open AND the stored
  value is unchanged since detection (reschedule guard). `stg_user_priorities`
  is never mutated; corrections auto-resolve when task and feed re-agree.
- Alerts ("Pipeline Alerts" → Jamil): once per entry per breakage — new
  anomaly or an open one whose stored value changed and is still wrong.
  1h grace on fresh feed events: a disagreement seen minutes after a
  remove/reschedule is Swift still propagating, not an anomaly — it only
  flags if it persists into the next run.
  `--notify-schedulers` additionally emails the scheduler ("Ontel Schedule
  Check" mask, directory-matched address; unique first+last match required).
- Modes: `--mode incremental` (open anomalies + schedules in a −3d/+45d
  window) / `--mode full` (~5.2k feed fetches, ~4 min). Coverage <90% ⇒ run
  FAILED (never silently green). Logs carry counts/DIDs only — no member
  names (public repo).
- Scheduling: `.github/workflows/schedule-feed-audit.yml` — hourly
  incremental (at :17) + nightly full (07:17 UTC), runs with
  `--notify-schedulers` (member notices approved 2026-08-14). Also accepts
  `repository_dispatch` type `schedule-feed-audit` and manual dispatch.

### Other extractors

| Script | Output |
|---|---|
| `extract_aging.py` | Gmail-based AR aging (`stg_ar_aging`) |
| `extract_sales.py` | Gmail-based sales detail (`stg_sales_detail`) |
| `extract_calendar_events.py` | Google Calendar events → `stg_calendar_events` (all kinds: leave/holiday/birthday/training/other) — incremental, AI-normalized |
| `extract_daily_reports.py` | Daily reports + per-task work summaries. Rolling window (default 30 days) plus a stale-status sweep: out-of-window tasks still non-terminal in staging get a status-only refresh, so late batch approvals (>30 days after the work date) still land. A weekly Apps Script trigger (`triggerDailyReportsDeep`, Sunday 5-6 AM ET) re-runs the same workflow with a 90-day window to also catch late requirement/timer edits. |
| `extract_revenue_rates.py` | `reference.ref_task_revenue_rates` from a manually-maintained sheet |

## Key files (`swift_api_pipeline/`)

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point; orchestrates all pipelines |
| `config.py` | Configuration loader (Swift creds, DB, logging) |
| `db.py` | asyncpg pool with sync bridge (background event loop thread). Retries 3× on transient DNS blips. |
| `base_extractor.py` | Shared base for extractors (Swift auth, pipeline_runs tracking) |
| `transform.py` | All transformation logic (raw → staging), server-side SQL |
| `pipeline_notifier.py` | Email notifications via Gmail API |
| `pipeline.py` | Orgs/projects and user_priorities extraction |
| `gmail_client.py` | Gmail API authentication |
| `calendar_client.py` | Google Calendar API authentication |
| `sheets_client.py` | Drive API authentication (used for Google Forms responses) |
| `timer_correction_review.py` | Daily timer review emails + form-response apply (corrections/removals -> `app_timer.*` -> `rebuild_timer_clean()`). Since 2026-08-10 (#44): stale-response removals are surgical (duration-matched to the snapshot the member saw; no match = skip for manual review, never bulk group removal) and form prefill entry ids carry an `id:` prefix so Google Sheets can't mangle hex hashes into scientific notation. |
| `schedule_feed_audit.py` | Swift task-record vs activity-feed schedule audit (12h calendar-path flip); maintains `pipeline.schedule_audit_anomalies`, serves corrections via `analytics.v_user_priorities_effective` |
| `migrations/*.sql` | Numbered SQL migrations (000-064 at time of writing) |

## CLI

```bash
# Full pipeline (extract + transform + backfill + MV refresh)
python main.py

# Single pipeline
python main.py --pipeline asset_tasks
python main.py --pipeline forms
python main.py --pipeline timer
python main.py --pipeline orgs
python main.py --pipeline user_priorities
python main.py --pipeline targeted_asset_tasks   # OIR-scoped
python main.py --pipeline targeted_task_requirements
python extract_calendar_events.py                # calendar events (standalone, not a main.py pipeline)
python main.py --pipeline aging
python main.py --pipeline sales

# Extract / transform stages only
python main.py --extract
python main.py --transform

# Suppress email notifications
python main.py --no-email
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

`.env` in `swift_api_pipeline/`:

```env
SWIFT_EMAIL=<swift-login-email>
SWIFT_PASSWORD=...
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_HOST=<your-pooler-host>.pooler.supabase.com
SUPABASE_PORT=5432
SUPABASE_DB=postgres
SUPABASE_USER=postgres.<your-project-ref>
SUPABASE_PASSWORD=...
```

Gmail/Drive/Calendar OAuth tokens (`gmail_credentials/token.pickle`,
`credentials.json`, etc.) are required for the notifier + the
Gmail/Calendar/Sheets pipelines. In GHA they're injected from secrets
(`NOTIFIER_*`, `CALENDAR_TOKEN_PICKLE`, `SHEETS_TOKEN_PICKLE`).

## GHA secrets (in `local-pipeline`)

| Secret | Used by |
|---|---|
| `SWIFT_PASSWORD`, `SUPABASE_PASSWORD` | All pipelines |
| `NOTIFIER_CREDENTIALS_JSON`, `NOTIFIER_TOKEN_PICKLE` | All pipelines (Gmail send via the notifier account) |
| `SHEETS_TOKEN_PICKLE` | Timer-discrepancies, timer-correction-apply, timer-duplicate-resolve |
| `CALENDAR_TOKEN_PICKLE` | Calendar-events pipeline |
| `DATE_VALIDATOR_DISPATCH_PAT` | Cross-repo dispatches (date-validator, report-automation) |

## Database migrations

`swift_api_pipeline/migrations/` holds numbered SQL files. Apply via
Supabase MCP `apply_migration` or `psql`. Migrations are
versioned 000+ at time of writing.

See `migrations/` for the full history. Run `git log --oneline
migrations/` for recent changes.

## Performance (typical nightly)

The asset_tasks pipeline is the longest-running step (tens of minutes
on a GHA runner). QA forms and the targeted extractors are an order of
magnitude faster. Timer, priorities, and analytics MV refresh complete
in minutes. Backfill steps take well under a minute.

## Related repos

- `report-automation/` — consumes data from this pipeline to generate
  weekly reports (daily finance, weekly compliance, open items)
- `date-validator/` — fired by this pipeline's `pipeline-asset-tasks`
  end-of-run dispatch; cross-checks Swift task dates against Gmail
  email dates
- `gmail-scraper/` — separate ETL that feeds package emails into
  Supabase; consumed by the date-validator
