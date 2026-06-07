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
Script time-driven triggers under `jamil.mendez@nanoninth.com`. See
`scripts/pipeline_trigger.gs` for the full schedule.

| Workflow | Trigger | What it refreshes |
|---|---|---|
| `pipeline-orgs.yml` | Apps Script 22:13 ET | Orgs + projects (Phase 1, must run first) |
| `pipeline-timer.yml` | Apps Script 00:09 ET | `stg_timer_activities` + `stg_timer_corrections` apply + clean rebuild |
| `pipeline-priorities.yml` | Apps Script 00:13 ET | `stg_user_priorities` |
| `pipeline-forms.yml` | Apps Script 00:17 ET | `stg_qa_form` |
| `pipeline-timer-discrepancies.yml` | Apps Script 00:21 ET | Google Form → `stg_timer_discrepancies` |
| `pipeline-calendar-leave.yml` | Apps Script 00:30 ET | Google Calendar → `stg_calendar_leave` (incremental, AI-normalized) |
| `pipeline-asset-tasks.yml` | Apps Script 00:01 ET | Heavy nightly: 2.5M+ asset_tasks → MVs → fires 4 downstream dispatches |
| `pipeline-asset-tasks-gc.yml` | Apps Script 02:00 ET | Parallel GC pipeline (~294 non-Ontel orgs) |
| `pipeline-open-items-data.yml` | Apps Script 02:00–03:00 ET | OIR-scoped Swift snapshots + cross-repo dispatch to report-automation |
| `gmail-pipeline.yml` | Apps Script gmail_trigger.gs every 5 min | AR aging + sales detail when Daily Revenue Report email arrives |
| `timer-correction-apply.yml` | Apps Script onFormSubmit | Apply timer-duration corrections in real time |
| `timer-duplicate-resolve.yml` | Apps Script onFormSubmit | Resolve timer-duplicate reviews in real time |

The `pipeline-asset-tasks` workflow fires four `repository_dispatch`
downstream events at end-of-run: `pipeline-asset-tasks-export`,
`pipeline-timer-discrepancies`, `date-validator-daily` (cross-repo to
`date-validator`), and `weekly-compliance-audit` (cross-repo to
`report-automation`, Fridays only). The `pipeline-open-items-data`
workflow fires `open-items-report-monday` or `open-items-report-friday`
to `report-automation` based on day-of-week.

Cross-repo dispatches use the `DATE_VALIDATOR_DISPATCH_PAT` secret.

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
│   ├── extract_forms.py → raw_form_qa_ts13..ts18 → stg_qa_form
│   └── extract_timer.py → raw_timer_activities → stg_timer_activities
│       └── data_staging.rebuild_timer_clean() → stg_timer_activities_clean
│
└── Post-Phase 2:
    ├── data_staging.backfill_asset_did() (3-pass: asset_id → asset_name → FA regex)
    └── analytics.refresh_one_mv() × 3 (mv_project_summary, mv_technician_stats, mv_daily_completion)
```

### Targeted extractors (report-driven)

Lightweight pipelines that snapshot Swift data for specific
`(org, project)` sets defined in `reference.report_targets`. Used by
report-automation reports that don't need full-scale extracts.

| Extractor | Output | Used by |
|---|---|---|
| `extract_targeted_asset_tasks.py` | `data_staging.stg_targeted_asset_tasks` (~133k OIR scope) | Open Items Report (BetaSites Final COP date) |
| `extract_targeted_task_requirements.py` | `data_staging.stg_targeted_task_requirements` (~655 OIR scope) | Open Items Report (per-requirement detail) |

`stg_targeted_asset_tasks` captures both `task_approved_on` and
`task_submitted_on` from Swift's `approvedOn`/`submittedOn` epoch fields.

### Other extractors

| Script | Output |
|---|---|
| `extract_aging.py` | Gmail-based AR aging (`stg_ar_aging`) |
| `extract_sales.py` | Gmail-based sales detail (`stg_sales_detail`) |
| `extract_calendar_leave.py` | Google Calendar leave events (`stg_calendar_leave`) — incremental, AI-normalized |
| `extract_daily_reports.py` | Daily reports + per-task work summaries |
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
python main.py --pipeline calendar_leave
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
SWIFT_EMAIL=mgmt@ontel.co
SWIFT_PASSWORD=...
SUPABASE_URL=https://voqfjfngdpcvevbkikud.supabase.co
SUPABASE_HOST=aws-0-ap-southeast-1.pooler.supabase.com
SUPABASE_PORT=5432
SUPABASE_DB=postgres
SUPABASE_USER=postgres.voqfjfngdpcvevbkikud
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
| `NOTIFIER_CREDENTIALS_JSON`, `NOTIFIER_TOKEN_PICKLE` | All pipelines (Gmail send via jamil.mendez@nanoninth.com) |
| `SHEETS_TOKEN_PICKLE` | Timer-discrepancies, timer-correction-apply, timer-duplicate-resolve |
| `CALENDAR_TOKEN_PICKLE` | Calendar-leave pipeline |
| `DATE_VALIDATOR_DISPATCH_PAT` | Cross-repo dispatches (date-validator, report-automation) |

## Database migrations

`swift_api_pipeline/migrations/` holds numbered SQL files. Apply via
Supabase MCP `apply_migration` or `psql`. Migrations are
versioned 000+ at time of writing.

Recent additions:
- `059_report_group_meta.sql` — OIR display config
- `060_targeted_asset_tasks_approved_on.sql` — `task_approved_on` column
- `061_open_items_views.sql` — `analytics.v_open_items_*` views
- `062_open_items_carrier_column.sql` — `carrier` (ATT/TMO/VZW) on `report_group_meta`
- `063_targeted_asset_tasks_submitted_on.sql` — `task_submitted_on` column
- `064_open_items_views_use_submitted_on.sql` — OIR view keys on `submittedOn`

## Performance (typical nightly)

| Operation | Duration |
|-----------|----------|
| Asset tasks (extract + transform on GHA) | ~25–30 min |
| QA forms | ~10–15 min |
| Timer activities | ~30 sec |
| User priorities | ~2 min |
| Targeted asset tasks (OIR scope only) | ~3–5 min |
| Analytics MV refresh | ~1–2 min total |
| Backfill asset_did (3-pass) | ~30–60 sec |

## Data volumes (approximate)

| Table | Rows |
|-------|------|
| stg_asset_tasks | ~2.5M |
| stg_qa_form | ~383K |
| stg_timer_activities | ~283K |
| stg_targeted_asset_tasks | ~133K (OIR scope) |
| stg_user_priorities | ~12K |
| stg_assets | ~33K |
| stg_calendar_leave | ~10K |
| stg_timer_discrepancies | ~5K |

## Related repos

- `report-automation/` — consumes data from this pipeline to generate
  weekly reports (daily finance, weekly compliance, open items)
- `date-validator/` — fired by this pipeline's `pipeline-asset-tasks`
  end-of-run dispatch; cross-checks Swift task dates against Gmail
  email dates
- `gmail-scraper/` — separate ETL that feeds package emails into
  Supabase; consumed by the date-validator
