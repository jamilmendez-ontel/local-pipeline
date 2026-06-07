# Swift API Pipeline

Inner directory of the `local-pipeline` repo. Holds the actual ETL code,
SQL migrations, and per-pipeline orchestration. See the
[repo root README](../README.md) for the bird's-eye view.

## What lives here

| Path | What |
|---|---|
| `main.py` | CLI entry point — orchestrates all pipelines |
| `config.py` | Configuration loader |
| `db.py` | asyncpg pool + sync bridge |
| `base_extractor.py` | Shared base class for extractors |
| `transform.py` | Raw → staging transformations |
| `pipeline_notifier.py` | Email notifications via Gmail API |
| `pipeline.py` | Orgs/projects + user_priorities extraction |
| `extract_*.py` | Per-pipeline extractors (asset_tasks, forms, timer, etc.) |
| `gmail_client.py` / `calendar_client.py` / `sheets_client.py` | Google API auth |
| `migrations/` | Numbered SQL migrations (000+) |
| `daily_reports_export/` | Schema metadata exports for DARA |
| `docs/` | Schema docs, dbml, DARA project prompt |
| `pipeline_logs/` | Per-run log files (gitignored) |
| `gmail_credentials/` | OAuth tokens (gitignored; populated from GHA secrets in CI) |
| `venv/` | Local Python virtualenv (gitignored) |
| `requirements.txt` | pinned deps |
| `.env` | Local secrets (gitignored) |

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create `.env`:

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

For pipelines that need Gmail/Calendar/Drive (notifier, aging, sales,
calendar_leave, timer-discrepancies), place the corresponding pickle
files under `gmail_credentials/` (see repo root README for which
pipelines need which token).

## CLI usage

```bash
# Full pipeline (Phase 1 → Phase 2 parallel → backfill → MV refresh)
python main.py

# Single pipeline
python main.py --pipeline asset_tasks
python main.py --pipeline forms
python main.py --pipeline timer
python main.py --pipeline orgs
python main.py --pipeline user_priorities
python main.py --pipeline targeted_asset_tasks
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

## Architecture (medallion-ish)

```
data_raw ─► data_staging ─► analytics
(JSONB)    (normalized)    (views + MVs)
```

The big pipelines (`asset_tasks`, `forms`, `timer`) keep their raw JSONB
in `data_raw.*` for replay/audit. The lighter targeted pipelines
(`targeted_asset_tasks`, `targeted_task_requirements`) skip the raw
layer and write directly to `data_staging.*` — the responses are small
enough that snapshot-reload is cheap enough to re-fetch on demand.

## Pipeline runs metadata

Every pipeline writes to `pipeline.pipeline_runs` with a `run_id`,
status, start/end times, and row counts. Used by the notifier email
body and by downstream consumers needing freshness checks.

```sql
SELECT run_id, pipeline_name, status,
       EXTRACT(EPOCH FROM (completed_at - started_at)) AS duration_s,
       records_extracted
FROM pipeline.pipeline_runs
ORDER BY started_at DESC
LIMIT 10;
```

## Notable transformation logic

- `data_staging.backfill_asset_did()` — 3-pass matcher (asset_id →
  asset_name → FA regex) that links timer + QA-form rows to the
  canonical asset DID. Pass-0 restores from `qa_form_asset_did_lookup`
  for QA forms (cumulative map; never loses mappings).
- `data_staging.rebuild_timer_clean()` — TRUNCATE + INSERT excluding
  rejected entries (from duplicate review) and applying corrections
  (from `stg_timer_corrections`). Idempotent.
- `analytics.refresh_one_mv(p_view_name)` — refreshes one MV at a time
  (~12–34 s each).
- `analytics.refresh_invoice_audit()` — used by the weekly compliance
  audit report; auto-syncs new TS<n> projects.

## Migrations

See the repo root README for the recent additions list. To apply a new
migration:

```bash
# Either via Supabase MCP apply_migration, or:
psql "$DATABASE_URL" -f migrations/<migration-file>.sql
```

Migrations are append-only; if you need to change something already
shipped, write a new migration that overrides it.

## Testing

```bash
pytest                              # full suite
pytest tests/test_specific.py       # one module
pytest -k "asset_did" -v            # by name pattern
```

Most tests are integration tests against a dev Supabase project. There
is no isolated unit-test suite for the extractors — they live or die
with the live API.

## Logs

Per-run log files in `pipeline_logs/`. Each pipeline gets its own
timestamped file. The notifier email attaches just that pipeline's log.
On GHA, logs are uploaded as workflow artifacts on failure.

## Troubleshooting

- **Asset tasks timeout** — see internal incident docs for the
  atomic-transform + extended-timeout fix.
- **Pool exhaustion (EMAXCONNSESSION)** — caused by overlapping nightly
  jobs hitting the pooler's session cap. Stagger schedules out of the
  busiest window.
- **Token expiry** — Gmail/Drive/Calendar tokens. The GCP project is
  published to Production to avoid Testing-mode token expiry. If a
  token does expire, re-run the OAuth flow locally and re-base64 the
  pickle into the GHA secret.
