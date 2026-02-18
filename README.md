# Swift API Data Pipeline

ETL pipeline that extracts data from the Swift Projects API and Gmail, transforms it into clean staging tables, and maintains analytics views in Supabase (PostgreSQL).

## Architecture

```
Swift API ──┐
            ├──► data_raw (JSONB) ──► data_staging (normalized) ──► analytics (views/MVs)
Gmail ──────┘
```

### Database Schemas

| Schema | Purpose |
|--------|---------|
| `data_raw` | Raw API responses stored as JSONB |
| `data_staging` | Cleaned, normalized staging tables |
| `analytics` | Pre-joined views + materialized views for reporting |
| `pipeline` | Run tracking metadata (pipeline_runs table) |

### Pipeline Phases

**Phase 1 — Organizations & Projects** (sequential)
- Extracts org/project data from Swift API
- Must run first (other pipelines depend on reference data)

**Phase 2 — Parallel Extraction** (4 pipelines via ThreadPoolExecutor)
- **Asset Tasks** — 2.2M+ rows across 6 projects, 6 parallel API workers per project
- **User Priorities** — User priority assignments
- **QA Forms** — 348K+ inspection form responses
- **Timer Activities** — Time tracking entries (append mode)

**Post-Phase 2** (sequential)
- **Asset DID Backfill** — Links timer/QA form rows to assets via `backfill_asset_did()` RPC
- **Analytics MV Refresh** — Refreshes 3 materialized views (`mv_project_summary`, `mv_technician_stats`, `mv_daily_completion`)

**Gmail Pipelines** (separate scheduler, every 30 min)
- **AR Aging** — Parses aging report from Daily Revenue Report email
- **Sales Detail** — Parses sales data from same email

Each pipeline sends its own email notification with log attachment and relevant row counts.

## Pipeline Flow

```
main.py
├── Phase 1: Organizations & Projects
│   ├── pipeline.py → extract orgs/projects
│   └── transform.py → stg_organizations, stg_projects
│
├── Phase 2: Parallel (ThreadPoolExecutor)
│   ├── Asset Tasks
│   │   ├── extract_asset_tasks.py → raw_asset_tasks (6 workers, COPY protocol)
│   │   └── transform.py → stg_assets (RPC), stg_asset_tasks (server-side SQL)
│   ├── User Priorities
│   │   ├── pipeline.py → raw_user_priorities
│   │   └── transform.py → stg_user_priorities
│   ├── QA Forms
│   │   ├── extract_forms.py → raw_form_qa_ts13..ts18
│   │   └── transform.py → stg_qa_form (server-side SQL)
│   └── Timer Activities
│       ├── extract_timer.py → raw_timer_activities
│       └── transform.py → stg_timer_activities
│
└── Post-Phase 2:
    ├── backfill_asset_did() — 3-pass matching (asset_id → asset_name → FA regex)
    └── refresh_analytics() — mv_project_summary, mv_technician_stats, mv_daily_completion
```

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — orchestrates all pipelines with CLI options |
| `config.py` | Configuration loader (API credentials, DB, logging, schema names) |
| `db.py` | Asyncpg connection pool with sync bridge (background event loop thread) |
| `base_extractor.py` | Shared base class for extractors (auth, pipeline tracking) |
| `transform.py` | All transformation logic (raw → staging), server-side SQL |
| `pipeline_notifier.py` | Email notifications via Gmail API with log capture |
| `pipeline.py` | Org/project and user priority extraction |
| `extract_asset_tasks.py` | Asset task extraction (6 parallel workers, COPY protocol) |
| `extract_forms.py` | QA form extraction (multi-form support) |
| `extract_timer.py` | Timer activity extraction (append mode) |
| `extract_aging.py` | Gmail-based AR aging extraction |
| `extract_sales.py` | Gmail-based sales detail extraction |
| `gmail_client.py` | Gmail API authentication and email search |
| `run_gmail_pipelines.py` | Gmail pipeline scheduler (checks for new emails before running) |

## Usage

```bash
# Full pipeline (extract + transform + backfill + MV refresh)
python main.py

# Extract only
python main.py --extract

# Transform only (uses latest successful extractions)
python main.py --transform

# Single pipeline
python main.py --pipeline asset_tasks
python main.py --pipeline forms
python main.py --pipeline timer
python main.py --pipeline orgs
python main.py --pipeline user_priorities
python main.py --pipeline aging
python main.py --pipeline sales

# Suppress email notifications
python main.py --no-email
```

## Automation (Windows Task Scheduler)

| Task | Schedule | Script | Description |
|------|----------|--------|-------------|
| SwiftPipeline-Nightly | Daily 12:01 AM | `scheduled_main_pipeline.bat` | Full pipeline + Excel export |
| SwiftPipeline-Gmail-Hourly | Every 30 min, 1-10 AM | `scheduled_gmail_pipeline.bat` | Gmail polling (aging + sales) |

Both run as `admin` user in background mode. Logs written to `pipeline_logs/`.

## Setup

```bash
# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Variables

Create `.env` in `swift_api_pipeline/`:

```env
SWIFT_EMAIL=your.email@company.com
SWIFT_PASSWORD=your_password
SUPABASE_HOST=db.your-project.supabase.co
SUPABASE_PORT=5432
SUPABASE_DB=postgres
SUPABASE_USER=postgres
SUPABASE_PASSWORD=your_password
```

Gmail API requires `credentials.json` and `token.json` in the pipeline directory (OAuth2 for sending notifications and reading Daily Revenue Report emails).

## Database Migrations

Migrations are in `migrations/` directory (numbered 001-024). Apply with:

```bash
# Most migrations are SQL files applied via Python helpers
python migrations/apply_024.py
```

## Email Notifications

Each pipeline step sends an individual email with:
- Status (SUCCESS/FAILED) with duration
- Before/after row counts for that pipeline's tables only
- Log file attachment (filtered to only show that pipeline's logs)

## Performance

| Operation | Duration |
|-----------|----------|
| Full pipeline (overnight) | ~25-35 min |
| Asset tasks extraction (2.2M rows) | ~40 min |
| Asset tasks transform (server-side SQL) | ~2 min |
| QA Forms extraction + transform | ~30 min + 12 min |
| Timer extraction + transform | ~17 sec |
| Analytics MV refresh | ~1-2 min total |

## Data Volumes

| Table | Rows |
|-------|------|
| raw_asset_tasks / stg_asset_tasks | ~2.2M |
| stg_qa_form | ~348K |
| stg_timer_activities | ~283K |
| stg_user_priorities | ~104K |
| stg_assets | ~29K |
