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
| `pipeline-timer.yml` | Apps Script ~1:15 AM ET (13:15 PHT) | Run 1 of 2: `stg_timer_activities` reload + asset_did backfill + Excel exports (raw + clean, Drive + email; Sheena's report reads the 13:15 PHT clean export) + corrections apply + clean rebuild + MV refresh. **No member emails** since 2026-08-28 |
| `pipeline-timer-emails.yml` | Apps Script ~6:00 AM ET (18:00 PHT, `triggerTimerEmails`) | Run 2 of 2: re-extract + backfill + apply/rebuild + MV refresh, then the member-facing emails (`--remind`, `--send`, `--resend`). Separate `pipeline-timer-emails` dispatch type; same `pipeline-timer` concurrency group. No exports |
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
| `holiday-feed-watch.yml` | Apps Script Mon 6 PM ET + cron Thu 22:00 UTC backstop | PH holiday proclamation watch: lawphil proclamation index (+ Official Gazette RSS when not 403-blocked) + Nager.Date vs `reference.ref_holidays`; emails Jamil proposed INSERT/UPDATE SQL (never writes the table), silent when nothing new (see below) |

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

**Timer data runs twice a day; member emails only on the second run (since 2026-08-28).**
A stop pressed in the Swift app can reach Swift's report data hours later (three
cases in Aug 2026, e.g. an 18:13 ET stop that was still NULL at the 01:19 ET
extract and only appeared after 01:27 ET). With extract and send 9 minutes apart,
the daily email flagged such timers as "still running" and the hours were short
until the next night. The fix is scheduling, not code: `pipeline-timer.yml`
(~1:15 AM ET / 13:15 PHT) is now data + exports only, and the new
`pipeline-timer-emails.yml` (~6:00 AM ET / 18:00 PHT, members' shift start)
re-extracts first and then runs `--remind` / `--send` / `--resend`, giving late
stops ~12 hours to land. The two workflows use different `repository_dispatch`
types (`pipeline-timer` vs `pipeline-timer-emails`, both from
`scripts/pipeline_trigger.gs`) and share the `pipeline-timer` concurrency group
with `timer-correction-apply.yml` so no two of them ever overlap. Cost: one more
raw snapshot per day (~7 MB); staging/clean are replaced in place. Nothing in the
timer pipeline compares one run to the previous one (the `>10%` drop baseline is
asset-tasks only), so the second run is safe. Known property: `--send` upserts
`daily_notifications` and re-sends if run twice on the same day, so the emails
run has no retry loop around the send steps.

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
  migrations 236-238. Classes: `timed_mismatch`, `ghost_schedule`
  (feed says removed, task still scheduled), `no_feed_schedule`.
  Date-only midnight-ET (task) vs noon-ET (feed) is benign and excluded.
  A remove whose leftover record value is exactly the 12h flip of the
  removed TIMED value counts as `timed_mismatch`, not ghost (2026-08-20
  TENASKA finding: such removes are half-applied - the schedule stays live
  in one store, so members see the flip symptom and the feed value is the
  truth to serve).
- Serving: `analytics.v_user_priorities_effective` overlays
  `scheduled_effective` (feed value) while an anomaly is open AND the stored
  value is unchanged since detection (reschedule guard). `stg_user_priorities`
  is never mutated; corrections auto-resolve when task and feed re-agree.
- Alerts ("Pipeline Alerts" → Jamil): once per entry per breakage — new
  anomaly or an open one whose stored value changed and is still wrong.
  The registry row is the alert queue (2026-08-20 review): `ops_alerted_at`
  / `notice_sent_at` / `resolved_notice_sent_at` stamp only on successful
  send and reset on reopen/re-break, so failed sends retry on later runs
  instead of being lost (migrations 238-239).
  Fresh feed events (<1h old) get an in-run recheck instead of a next-run
  punt (changed 2026-08-20, was a skip-to-next-run grace): wait 120s,
  re-pull the report, re-classify; still inconsistent ⇒ alert in the same
  run. Propagation false alarms stay suppressed, worst-case alert lag drops
  from ~2h to one run cycle.
  `--notify-schedulers` additionally sends a member-facing notice ("Ontel
  Schedule Check" mask; added 2026-08-20 after a remove-then-reschedule
  left a member's task falsely "Overdue" with only the ops email firing).
  Audience for both classes: the notice team (all active Data Analysts +
  Project Associates, LIVE from `analytics.v_employee_directory`) plus the
  directory-matched scheduler (unique first+last match). Wording approved
  via inbox samples 2026-08-20. When a noticed anomaly resolves,
  an all-clear follow-up replies on the SAME Gmail thread (thread ids on
  the anomaly row, migration 238), so members know the fix landed.
- Modes: `--mode incremental` (open anomalies + schedules in a −3d/+45d
  window) / `--mode full` (~5.2k feed fetches, ~4 min). Coverage <90% ⇒ run
  FAILED (never silently green). Logs carry counts/DIDs only — no member
  names (public repo).
- Scheduling: Apps Script `triggerScheduleFeedAudit()` every 15 min
  (primary since 2026-08-20, alarm-ASAP; `scripts/pipeline_trigger.gs`,
  create via `setupScheduleFeedAuditTrigger()`) →
  `.github/workflows/schedule-feed-audit.yml`, which also keeps an hourly
  GHA cron backstop (at :17) + nightly full (07:17 UTC). Runs with
  `--notify-schedulers` (member notices approved 2026-08-14). Double-fires
  are harmless: the script's overlap guard exits the second run (orphaned
  'running' rows reaped after 45 min). If no full run succeeded in 26h the
  next incremental self-upgrades to full (cron-drift/supersede insurance).

### Other extractors

| Script | Output |
|---|---|
| `extract_aging.py` | Gmail-based AR aging (`stg_ar_aging`) |
| `extract_sales.py` | Gmail-based sales detail (`stg_sales_detail`) |
| `extract_calendar_events.py` | Google Calendar events → `stg_calendar_events` (all kinds: leave/holiday/birthday/training/other) — incremental, AI-normalized |
| `extract_daily_reports.py` | Daily reports + per-task work summaries. Every run re-reads each asset's live Swift `shortName` (`FullName_<emp_id>`) and the `stg_daily_reports` upsert refreshes `asset_name` on conflict (`emp_id` stays as first derived, so a malformed rename can never break the roster join) (since 2026-08-28; before that only status fields were updated, so a Swift rename such as a married surname never reached pre-created rows). Rolling window (default 30 days) plus a stale-status sweep: out-of-window tasks still non-terminal in staging get a status-only refresh, so late batch approvals (>30 days after the work date) still land. A weekly Apps Script trigger (`triggerDailyReportsDeep`, Sunday 5-6 AM ET) re-runs the same workflow with a 90-day window to also catch late requirement/timer edits. |
| `extract_revenue_rates.py` | `reference.ref_task_revenue_rates` from a manually-maintained sheet |

### Holiday calendar + proclamation watch (2026-08-27)

`reference.ref_holidays` (migrations 244-246) is the one holiday table WITH
type: `calendar` PH / US / ONTEL, `holiday_type` regular /
special_non_working / special_working (PH, DOLE pay rules) / federal / company,
`is_non_working`, `proclamation_ref`, `amended_by`, `previous_type`. Key is
`(calendar, holiday_date)`; a proclamation that changes a day's type is an
UPDATE, and every UPDATE/DELETE is copied to `reference.ref_holidays_history`
by trigger. Seeded PH 2025-2026 (Proclamations 727 s.2024, 1006 s.2025, Eid
839/911/1189/1264), US federal 2025-2026, Ontel company days (copied once from
`scorecard.holidays`). Use it, not `analytics.v_calendar_holiday` (Google
Calendar mirror, incomplete, no type).

`holiday_feed_watcher.py` keeps it current without ever writing it. Weekly it
scans **lawphil.net's per-year proclamation index** (primary: number, signing
date, full title; plain nginx, reachable from GitHub runners; lags the Gazette
by ~2 weeks) plus, best effort, the **Official Gazette RSS** (fresher, but
Cloudflare in front of gov.ph returns 403 to GitHub runner IPs with any
User-Agent, probed 2026-08-27, so a 403 is a warning; when reachable it is
walked back to the last run's `coverage_ts` minus 21 days because the feed is
ordered by publish time, not number). Items are merged by key; a proclamation
is new when no earlier real run scanned its key
(`pipeline.holiday_watch_runs.scanned_keys`). The subject line is parsed
(type, dates, "THROUGHOUT THE COUNTRY" vs "IN THE MUNICIPALITY OF", annual
list, "AMENDING PROCLAMATION NO.") and Jamil gets the proposed INSERT/UPDATE
SQL by email. Mixed-type subjects, unparseable dates and Gazette coverage gaps
become "review" items, never SQL. Nager.Date is a dates-only cross-check for
seeded years, each date reported once. `--dry-run` never emails or advances
coverage; `--lookback-days N` re-walks the Gazette further back; `--probe`
prints each source's HTTP status from wherever it runs. Adding a year is still
a hand-seeded migration block (the watcher tells you when the annual list is
published).

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
| `timer_correction_review.py` | Daily timer review emails + form-response apply (corrections/removals -> `app_timer.*` -> `rebuild_timer_clean()`). Since 2026-08-10 (#44): stale-response removals are surgical (duration-matched to the snapshot the member saw; no match = skip for manual review, never bulk group removal) and form prefill entry ids carry an `id:` prefix so Google Sheets can't mangle hex hashes into scientific notation. Since 2026-08-24: still-running timers (`end_time IS NULL`) are not actionable rows; they get an amber "timer still running" notice (site/task, start, elapsed, an "Open in Swift" task deep link resolved via `stg_asset_tasks` (DRMC `swiftTaskUrl` pattern; root link when the timer has no asset), no Edit/Remove) and the completed entry arrives via the resend pass as NEW. Ghost NULL-end rows with a completed sibling are dropped silently. Migration 241 stops a NULL-end removal from shielding Step 5 runaway-duplicate cleanup. Design: `docs/superpowers/specs/2026-08-24-timer-running-entries-design.md`. |
| `schedule_feed_audit.py` | Swift task-record vs activity-feed schedule audit (12h calendar-path flip); maintains `pipeline.schedule_audit_anomalies`, serves corrections via `analytics.v_user_priorities_effective` |
| `holiday_feed_watcher.py` | Weekly PH holiday proclamation watch (lawphil index + best-effort Official Gazette RSS subject-line parser + Nager.Date cross-check + staleness) against `reference.ref_holidays`; emails proposed SQL, never writes the table; run log `pipeline.holiday_watch_runs` |
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
migrations/` for recent changes. Latest: 251 duplicate-survivor-bug restore (reverts the
14 `auto_resolved_sibling` removals the latest-end survivor pick wrote in zeroed
duplicate groups and re-points each review at the restored snapshot; applied live
2026-08-31, ~40.9h returned to `stg_timer_activities_clean`); 250 `reference.ref_pmi_clusters.source_sheet_id`
(lets a PMI market be sourced from a Google Sheet instead of the pending + failing
xlsx pair; NULL keeps the xlsx path, WBV is seeded); 247-249 Viaero market (rate rows cloned from
VZW Small Cell in `reference.ref_task_revenue_rates`; `Viaero` anchor + rule in
`reference.market_signature()` / `seed_new_market_signatures()`, 15 buckets; `Viaero` ->
`Verizon` row in `reference.ref_carrier_groups` + stg_assets backfill); 244-246
`reference.ref_holidays` (+ history trigger, `pipeline.holiday_watch_runs` with publish-time
coverage), see the holiday calendar section above; 243 Weekly PMI tracker; 242 `analytics.member_weekly_task_mix(p_from
date, p_to date)`, a set-returning function: completed-timer minutes per (member
email, ET ISO week start, task-type bucket) with single timers over 12h excluded
(runaway rule), one aggregate call for the ontel-people per-member weekly Timer &
Daily Report Summary packs. Classification is still `analytics.task_mix_category(text)`
from 240 (`analytics.v_timer_task_mix_daily`, ops report, drift-guarded by
ontel-people's `task-mix.sql-sync.test.ts`); 241 is the `rebuild_timer_clean()`
Step 5 fix described in the timer review row above.

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
