# Asset Tasks GC Pipeline — Design Spec

**Status:** Approved for planning (2026-05-20)
**Owner:** jamil.mendez@ontel.co
**Predecessor:** `docs/plans/2026-04-16-asset-tasks-gha-migration.md` (Ontel pipeline — already shipped through Task 5; Task 6 retires local batch ~2026-05-27)
**Implementation plan:** to be created next via the writing-plans skill

---

## 1. Overview

Build a second nightly ETL pipeline, parallel to the existing Ontel asset_tasks pipeline, that pulls asset_tasks data for **all non-Ontel organizations** (General Contractors) from the Swift Projects API into Supabase. Data lands in a separate set of `_gc`-suffixed tables, gets transformed into staging, and feeds parallel analytics materialized views — leaving the Ontel pipeline completely untouched.

The pipeline is **standard scope** (raw + staging + analytics MVs). No date validator integration, no daily export emails, no DARA-facing column-metadata changes in v1 — those are downstream extensions for later if/when a concrete need arises.

## 2. Scope & Non-Goals

### In scope
- Daily Swift API extraction of asset_tasks for all GC orgs and projects matching the filter `org_name != 'Ontel' AND org_name NOT LIKE 'Testing%'` (~294 orgs, ~1,063 projects, ~2M asset_tasks).
- **All project statuses included** — `in_progress`, `pending`, AND `complete`. Diverges from Ontel pipeline (which only pulls `in_progress` TS projects). Rationale: GC `complete` projects are historical/closeout snapshots whose data is useful for analytics and reporting and doesn't change frequently. Re-extracting them nightly is wasteful in API calls but harmless data-wise (same TRUNCATE+RELOAD pattern produces idempotent results).
- Single table `data_raw.raw_asset_tasks_gc` (unpartitioned, see §6 for the rationale this differs from Ontel's partitioned design)
- Transforms into `data_staging.stg_asset_tasks_gc` and `data_staging.stg_assets_gc`
- Three analytics MVs: `analytics.mv_project_summary_gc`, `mv_technician_stats_gc`, `mv_daily_completion_gc`
- New GHA workflow `.github/workflows/pipeline-asset-tasks-gc.yml` fired by Apps Script trigger at 02:00 AM ET nightly
- Reuse of all existing infrastructure (BaseExtractor, asyncpg pool, COPY pattern, retry_db helper, pipeline_runs tracking, partial-success behavior)

### Out of scope (for v1)
- Date validator integration — COP/48hr emails are Ontel-specific; GC has no equivalent package-email workflow
- Daily Excel/Sheets export of GC asset_tasks — can be added later if team asks
- DARA agent column-level metadata for the new tables (will be picked up by the existing schema_cache the next time it refreshes)
- Backfill of historical GC data — pipeline starts from today, no point-in-time reconstruction
- Per-carrier or per-market segmentation of the GC data
- A `reference.ref_gc_projects` curated table — auto-discovery from `stg_projects` is the source of truth

## 3. Data Sources & Auth

### Upstream tables (read-only)
- `data_staging.stg_projects` — refreshed nightly at 22:24 ET by the existing `orgs_projects_extract` pipeline. Provides the source-of-truth org/project list with `org_did`, `project_did`, `org_name`, `project_name`, `status`. **Filter at extraction time:** `org_name != 'Ontel' AND org_name NOT LIKE 'Testing%'` (all statuses included — see §2).

### Swift API endpoint
Same endpoint family the Ontel pipeline already uses:
```
GET https://prod.api.swiftprojects.io/api/next/projects/{project_did}/assets/_export
  ?pageSize=1000&dateFormat=yyyy-MM-dd&timezone=America/New_York
```
**Verified 2026-05-20 spot-check:** Same `mgmt@ontel.co` Swift auth that pulls Ontel projects successfully reads non-Ontel orgs' asset_tasks. HTTP 200, full page of 1,000 rows returned, identical row shape (`Project_DID`, `Asset_DID`, `Task_DID`, etc.). No new auth or permissions work needed.

### Credentials
Same secrets the Ontel pipeline uses — no new secrets:
- `SWIFT_PASSWORD` (Swift API auth)
- `SUPABASE_PASSWORD` (DB)
- `NOTIFIER_CREDENTIALS_JSON` + `NOTIFIER_TOKEN_PICKLE` (pipeline notification emails)

## 4. Architecture

```
┌─────────────────────────────┐
│ Apps Script (nanoninth.com) │
│  triggerAssetTasksGC()      │ at 02:00 AM ET daily
└───────────┬─────────────────┘
            │ repository_dispatch
            │   event_type='pipeline-asset-tasks-gc'
            ▼
┌──────────────────────────────────────────────────┐
│ GHA: .github/workflows/pipeline-asset-tasks-gc.yml│
│  ubuntu-latest, timeout 60 min                    │
└───────────┬──────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│ python main.py --pipeline asset_tasks_gc          │
│   ↓                                                │
│   AssetTaskGCExtractor:                            │
│     1. fetch active GC orgs/projects from         │
│        stg_projects (~1,068 projects)              │
│     2. parallel extract (12 workers) → COPY into   │
│        data_raw.raw_asset_tasks_gc                 │
│     3. per-org safety check + cleanup of           │
│        WHERE run_id != current_run                 │
│   ↓                                                │
│   transform.run_assets_gc_transform(run_id)        │
│     → DELETE+INSERT data_staging.stg_assets_gc     │
│   ↓                                                │
│   transform.run_asset_tasks_gc_transform(run_id)   │
│     → DELETE+INSERT data_staging.stg_asset_tasks_gc│
└───────────┬──────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────┐
│ python main.py --pipeline analytics_gc            │
│   (new variant; refreshes only the _gc MVs)        │
└──────────────────────────────────────────────────┘
```

Estimated nightly runtime: ~10–15 min extract + ~3 min transform + ~3 min MV refresh = **~20 min total**.

## 5. Schema

### `data_raw.raw_asset_tasks_gc`

```sql
CREATE TABLE data_raw.raw_asset_tasks_gc (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id      UUID NOT NULL,
    org_did     TEXT NOT NULL,
    project_did TEXT NOT NULL,
    data        JSONB NOT NULL
);

CREATE INDEX idx_raw_asset_tasks_gc_run_id
    ON data_raw.raw_asset_tasks_gc (run_id);

CREATE INDEX idx_raw_asset_tasks_gc_org_did
    ON data_raw.raw_asset_tasks_gc (org_did);

CREATE INDEX idx_raw_asset_tasks_gc_loaded_at
    ON data_raw.raw_asset_tasks_gc (loaded_at DESC);
```

**Differences from Ontel's `raw_asset_tasks`:**
1. **`org_did` column added** — Ontel doesn't carry org_did because it's implicit (Ontel-only). GC needs it for per-org safety check and downstream filtering.
2. **No partitioning** — see §6 for the rationale. At 2M rows with narrow index columns, the Supabase pooler timeout that justified Ontel's partitioning doesn't apply.

The `data` JSONB column carries the same Swift API response shape as Ontel: `Project_DID`, `Project_Status`, `Asset_DID`, `Asset_ID`, `Asset_Name`, `Asset_Address`, `Asset_Latest_Message`, `Task_DID`, `Task_Name`, `Task_Status`, etc.

### `data_staging.stg_asset_tasks_gc`

Same column shape as `stg_asset_tasks`. Aggregation transform reads `raw_asset_tasks_gc.data` JSONB, flattens each task into a row, deduplicates by `(project_did, asset_did, task_did)`, and writes to staging via DELETE+INSERT (full refresh per run). Existing `task_name_clean` derivation reused.

### `data_staging.stg_assets_gc`

Same column shape as `stg_assets`. Aggregation rollup from `raw_asset_tasks_gc` — one row per `(project_did, asset_did)` with task counts grouped by status.

### Analytics MVs (mirror existing)

- `analytics.mv_project_summary_gc` — same SQL as `mv_project_summary` but sourcing `stg_asset_tasks_gc` + `stg_assets_gc`
- `analytics.mv_technician_stats_gc` — same pattern
- `analytics.mv_daily_completion_gc` — same pattern

Refreshed via the existing `analytics.refresh_one_mv()` RPC; the pipeline's `--pipeline analytics_gc` variant only refreshes the `_gc` MVs (Ontel MVs untouched).

## 6. Why a Single Unpartitioned Table (Diverges from Ontel)

The Ontel pipeline partitions `raw_asset_tasks` by `project_did` because the 2.5M-row table hit Supabase's connection-pooler 5–6 min ceiling during `CREATE INDEX` operations. Partitioning made each index a per-partition operation (~350K rows, completes in seconds).

The GC pipeline does NOT need partitioning because:

1. **Volume:** ~2M rows total — narrow index columns (BIGINT id, UUID run_id, TEXT org_did, TIMESTAMPTZ loaded_at) on 2M rows index in well under 60 sec. No timeout risk.
2. **Query patterns favor no partitioning:** The transform reads `WHERE run_id = $1` with no `org_did` predicate. Postgres partition pruning would buy nothing — the planner has to touch all partitions anyway, and at 296 partitions the planner overhead is measurable parse/plan cost on every query.
3. **DDL churn:** New GC orgs arrive monthly+ (Swift adds them to the platform). A partitioned design would mean DDL (CREATE TABLE + 2× CREATE INDEX) for every new org, every run. Unpartitioned eliminates that entirely — new orgs flow in via INSERT.
4. **Cleanup pattern:** `DELETE WHERE org_did = ANY($1) AND run_id != $2` with a composite index on `(org_did, run_id)` is an index scan, not a table scan. Fast.

This was a deliberate design decision validated by an independent code-architect review on 2026-05-20.

## 7. Extraction Logic

### `swift_api_pipeline/extract_asset_tasks_gc.py`

New module mirroring `extract_asset_tasks.py` minus the partition machinery. Key class: `AssetTaskGCExtractor(BaseExtractor)` with `pipeline_name="asset_tasks_gc_extract"`.

**`get_gc_projects()`** — replaces Ontel's `get_project_dids()`:
```python
SELECT org_did, org_name, project_did, project_name, status
FROM data_staging.stg_projects
WHERE org_name != 'Ontel'
  AND org_name NOT LIKE 'Testing%'
ORDER BY org_name, project_name
```

Returns ~1,063 rows across ~294 orgs (any status — `in_progress`, `pending`, or `complete`).

We intentionally do **not** filter on `asset_task_count > 0`. The counter in `stg_projects` is refreshed nightly by `orgs_projects_extract` and may briefly lag real Swift API state. Projects with a stale zero count cost one wasted API call (returns 0 rows fast) — cheaper than risking missed data.

**`extract_and_load_project(org_did, project_did, project_name)`** — same pagination + COPY pattern as Ontel, but the COPY includes the `org_did` column:
```python
columns=["run_id", "org_did", "project_did", "data"]
```

**`prepare_table_for_bulk_load()`** — drops the two write-path indexes (`run_id`, `loaded_at`) before bulk load. Leaves the `org_did` index up (used only for cleanup DELETE/COUNT, never on insert hot path).

**`restore_table_after_load()`** — recreates the two dropped indexes. Single CREATE INDEX per index, ~30–45 sec each on 2M rows. Well inside the pooler ceiling.

**`clear_old_raw_data(successful_orgs: list[str])`** — single batched DELETE:
```python
DELETE FROM data_raw.raw_asset_tasks_gc
WHERE org_did = ANY($1) AND run_id != $2
```
Passes the list of org_dids that passed the per-org safety check. Failed orgs keep their prior data untouched (same fall-back behavior as Ontel's partial-success path).

**Per-org safety check** — for each org that extracted successfully, compare `new_count` to `old_count` (rows in `raw_asset_tasks_gc WHERE org_did = X` excluding the current run). If `new_count < 0.90 * old_count`, skip that org's cleanup; record in `skipped_cleanups` for the run summary. Same 90% threshold as Ontel, just scoped per-org instead of per-project.

**Parallelism** — 12 workers (up from Ontel's 6). 1,068 projects × ~1.5 pages avg = ~1,600 API calls. At 6 workers, ~6 min serial; at 12 workers, ~3 min. The Swift API's per-IP rate limit is generous; existing retry-on-503 logic handles bursts.

### `swift_api_pipeline/main.py` additions

Three new `--pipeline` argparse choices, mirroring the Ontel split pattern:
- `asset_tasks_gc` — full extract + transform (combined)
- `asset_tasks_gc_extract` — extract only
- `asset_tasks_gc_transform` — transform only (looks up the latest `asset_tasks_gc_extract` run_id from `pipeline.pipeline_runs`)
- `analytics_gc` — refresh only the `_gc` MVs (`mv_project_summary_gc`, `mv_technician_stats_gc`, `mv_daily_completion_gc`)

The combined `asset_tasks_gc` choice calls (in order): `run_asset_task_gc_pipeline()` (extract + inline transforms via `run_assets_gc_transform` then `run_asset_tasks_gc_transform`).

### `swift_api_pipeline/transform.py` additions

Two new functions mirroring the existing Ontel ones:
- `run_assets_gc_transform(run_id=None)` — DELETE+INSERT `stg_assets_gc`, using a new RPC `data_staging.aggregate_assets_gc(p_run_id UUID)` that mirrors the existing aggregation but reads `raw_asset_tasks_gc`
- `run_asset_tasks_gc_transform(run_id=None)` — DELETE+INSERT `stg_asset_tasks_gc`, using a new RPC `data_staging.transform_asset_tasks_gc(p_run_id UUID)`

Both functions look up the latest successful `asset_tasks_gc_extract` run_id from `pipeline.pipeline_runs` if `run_id` is None.

### `swift_api_pipeline/pipeline_notifier.py` additions

New `PIPELINE_TABLES` entries so the row-count comparison table renders in notification emails:
- `"Asset Tasks GC"` → `[("data_raw", "raw_asset_tasks_gc"), ("data_staging", "stg_assets_gc"), ("data_staging", "stg_asset_tasks_gc")]`
- `"Asset Tasks GC Extract"` → `[("data_raw", "raw_asset_tasks_gc")]`
- `"Asset Tasks GC Transform"` → `[("data_staging", "stg_assets_gc"), ("data_staging", "stg_asset_tasks_gc")]`
- `"Analytics GC MV Refresh"` → `[]` (no row-count diff; MV refresh is full rebuild)

## 8. GHA Workflow

### `.github/workflows/pipeline-asset-tasks-gc.yml`

Clone of `pipeline-asset-tasks.yml` with the following differences:
- Trigger: `repository_dispatch` type `pipeline-asset-tasks-gc` + `workflow_dispatch`
- Concurrency group: `pipeline-asset-tasks-gc`
- 60-min timeout (lower than Ontel's 90 min — GC runs faster)
- Steps:
  1. Resolve inputs (project recovery, dispatch_downstream — same pattern as Ontel)
  2. Checkout, Python setup, install deps, create .env, decode notifier credentials
  3. `python -u main.py --pipeline asset_tasks_gc` (full extract + inline transforms)
  4. `python -u main.py --pipeline analytics_gc --no-email` (MV refresh)
  5. Upload logs on failure
- **No downstream dispatches** — GC pipeline doesn't fire anything else in v1 (no equivalent of pipeline-asset-tasks-export or date-validator-daily for GC data)

### Apps Script trigger

New function in the existing `scripts/pipeline_trigger.gs`:

```javascript
/**
 * Trigger GC asset_tasks pipeline. Schedule daily at 02:00 AM EST,
 * well after Ontel's pipeline finishes (~01:00 ET post-Task-6).
 */
function triggerAssetTasksGC() {
  fireDispatch_('pipeline-asset-tasks-gc');
}
```

Time-driven trigger: 02:00 AM EST daily, function = `triggerAssetTasksGC`.

## 9. Migration

### `swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql`

Single migration creates:
1. `data_raw.raw_asset_tasks_gc` + 3 indexes (see §5)
2. `data_staging.stg_asset_tasks_gc` (full mirror of `stg_asset_tasks` columns)
3. `data_staging.stg_assets_gc` (full mirror of `stg_assets` columns)
4. RPC `data_staging.aggregate_assets_gc(p_run_id UUID)` — mirrors existing `aggregate_assets` but reads `raw_asset_tasks_gc`
5. RPC `data_staging.transform_asset_tasks_gc(p_run_id UUID)` — mirrors existing `transform_asset_tasks` but reads `raw_asset_tasks_gc`
6. MVs `analytics.mv_project_summary_gc`, `mv_technician_stats_gc`, `mv_daily_completion_gc` — same SQL as the Ontel MVs with `_gc` table sources

Applied via `apply_053.py` following the established pattern (asyncpg direct connection, `statement_timeout=0` for safety, pre-flight + post-verify).

The migration is purely additive — no existing tables touched. Safe to apply during business hours.

## 10. Operational Concerns

### Pipeline runtime budget
- Extract: ~10–15 min (1,068 projects × 12 workers × ~3 sec avg/project)
- Transform: ~3 min (smaller dataset than Ontel)
- Analytics MV refresh: ~3 min (~32K projects × 296 orgs of metadata)
- **Total nightly: ~20 min**, with 60-min GHA timeout giving 3× headroom

### Failure modes & handling
| Failure | Behavior |
|---|---|
| Swift API 503 storm | Existing retry_db + per-project 10-retry logic handles. Persistent failure marks individual orgs as `failed_orgs`, partial success continues. |
| One org's extraction fails | Per-org safety check skips that org's cleanup; old data retained. Pipeline marks `status='success'` with detail in `error_message`. |
| 90% threshold trips for an org | Cleanup skipped for that org only; logged at WARNING. Other orgs proceed normally. |
| Transform RPC fails | Pipeline raises; status='failed'; raw data preserved for re-run. |
| MV refresh fails | Logged but non-fatal (continue-on-error in GHA workflow step). |
| Apps Script trigger misses a night | Same fallback as Ontel: manual workflow_dispatch from the GHA UI. |

### Storage impact
~2M rows × ~3KB avg JSONB = ~6 GB raw + ~500 MB staging + ~50 MB analytics. Total ~6.5 GB. Comfortably within Supabase tier limits.

### Cost impact (GHA minutes)
~20 min/night × 30 nights = 600 min/month. Public repo = unlimited minutes; private would consume ~30% of the 2,000-min free monthly cap.

## 11. Implementation Sequence (high-level — full plan in writing-plans output)

1. **Migration 053** — create tables, indexes, RPCs, MVs
2. **Extract code** — `extract_asset_tasks_gc.py` + main.py argparse additions
3. **Transform glue** — `transform.run_assets_gc_transform` + `run_asset_tasks_gc_transform`
4. **Notifier wiring** — `PIPELINE_TABLES` entries
5. **Smoke test** — single small org against the new code (similar to Ontel's TS13 recovery smoke)
6. **GHA workflow** — `pipeline-asset-tasks-gc.yml`
7. **Apps Script trigger** — `triggerAssetTasksGC()` + time-driven trigger at 02:00 AM EST
8. **First production run + verification** — schedule a one-time remote agent at 02:30 AM ET on the first night to confirm parity

## 12. Open Questions

None. The Swift API access risk was verified by spot-check 2026-05-20.

## 13. Success Criteria

- `data_raw.raw_asset_tasks_gc` has ~2M rows after first nightly run, partitioned across ~296 distinct `org_did` values
- `stg_asset_tasks_gc` and `stg_assets_gc` match raw counts (validated via `validate_transform_counts` like Ontel)
- All three GC MVs refresh successfully and contain non-zero rows
- Nightly run completes within 60-min GHA timeout
- Ontel pipeline runs (the 00:01 ET batch and the 02:00 ET shakedown GHA) are unaffected by GC pipeline activity
- No new repo secrets, MCP connectors, or external services needed

---

**Plan:** see `docs/plans/2026-05-20-asset-tasks-gc-pipeline.md` (to be created next via the writing-plans skill).
