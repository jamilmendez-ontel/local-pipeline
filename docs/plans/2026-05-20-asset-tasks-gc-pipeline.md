# GC Asset Tasks Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a second nightly pipeline (parallel to the existing Ontel asset_tasks pipeline) that extracts asset_tasks for all non-Ontel General Contractor orgs from the Swift API into new `_gc`-suffixed tables, transforms into staging, and refreshes parallel analytics MVs.

**Architecture:** Single unpartitioned `data_raw.raw_asset_tasks_gc` table (~2M rows from ~294 GC orgs / ~1,063 projects); auto-discovery from `data_staging.stg_projects`; per-org safety check with batched cleanup; nightly at 02:00 AM ET via Apps Script + GHA. The existing Ontel pipeline is untouched throughout.

**Tech Stack:** PostgreSQL 15 (Supabase), Python 3.12, asyncpg, GitHub Actions (ubuntu-latest), Google Apps Script (existing nanoninth.com project)

**Spec:** `docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md` (commit `e921854`)

**Key Constraints:**
- Migration must be **additive only** — no existing tables, MVs, RPCs, or columns touched. Apply during business hours is safe.
- `extract_asset_tasks_gc.py` reuses `BaseExtractor` + asyncpg patterns but drops all partition-management code (`ensure_partitions_exist`, `_get_partition_names`, `_PARTITION_INDEX_SUFFIXES`).
- Per-org safety check (90% threshold) + batched cleanup `DELETE WHERE org_did = ANY($1) AND run_id != $2` (single statement, not one-per-org).
- GHA workflow has **NO downstream dispatches** in v1 (no export, no validator). Mirror the Ontel workflow's structure minus those steps.
- Smoke test must pass on a single GC org before the GHA workflow is enabled for nightly auto-trigger.
- The existing Ontel pipeline (00:01 ET local batch + 01:30 ET Apps Script GHA shakedown, soon-to-be 00:01 ET GHA after Task 6 of the Ontel migration) must remain unaffected. All testing on this plan goes through `workflow_dispatch` or local Python — never the prod schedule.

---

## File Structure

**Create:**
- `swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql` — additive migration: tables, indexes, RPCs, MVs
- `swift_api_pipeline/migrations/apply_053.py` — apply script (mirror of `apply_052.py`)
- `swift_api_pipeline/extract_asset_tasks_gc.py` — new extractor module
- `.github/workflows/pipeline-asset-tasks-gc.yml` — GHA workflow

**Modify:**
- `swift_api_pipeline/transform.py` — add `run_assets_gc_transform()` + `run_asset_tasks_gc_transform()`
- `swift_api_pipeline/main.py` — add `--pipeline asset_tasks_gc`, `asset_tasks_gc_extract`, `asset_tasks_gc_transform`, `analytics_gc` argparse choices + wrapper functions
- `swift_api_pipeline/pipeline_notifier.py` — add `PIPELINE_TABLES` entries for the new pipeline names
- `scripts/pipeline_trigger.gs` — add `triggerAssetTasksGC()` function

**Not touched:**
- `swift_api_pipeline/extract_asset_tasks.py` — Ontel extractor stays as-is
- `swift_api_pipeline/base_extractor.py`, `db.py`, `config.py` — shared infra reused unchanged
- Any existing migration, table, MV, RPC, or workflow file
- `scripts/gmail_trigger.gs` or other existing Apps Script files

---

## Task 1: Migration 053 — Create GC Tables, RPCs, and MVs (additive)

**Files:**
- Create: `swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql`
- Create: `swift_api_pipeline/migrations/apply_053.py`

This migration is purely additive — no existing objects are touched. Safe to apply any time.

- [ ] **Step 1: Verify next migration number is 053**

```bash
ls swift_api_pipeline/migrations/0*.sql | sort | tail -5
```

Expected: highest is `052_partition_raw_asset_tasks.sql`. Confirm 053 is unused.

- [ ] **Step 2: Inspect existing RPCs and MVs to mirror**

Read these to understand the SQL shape the new GC versions must match:
- `swift_api_pipeline/migrations/014_aggregate_assets_rpc.sql` — pattern for `aggregate_assets_from_raw(p_run_id)`
- `swift_api_pipeline/migrations/021_analytics_schema.sql` lines 170–310 — `mv_project_summary` / `mv_technician_stats` / `mv_daily_completion` definitions
- Any later migration that defines `data_staging.transform_asset_tasks(p_run_id)` (the asset-tasks RPC that `run_asset_tasks_transform` calls). Use `grep -rn "transform_asset_tasks" swift_api_pipeline/migrations/ | head -5` to find it.

The GC versions are character-for-character clones with `_gc` suffix on the source tables. Do **not** invent new logic — copy and substitute.

- [ ] **Step 3: Write the migration SQL**

Create `swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql`:

```sql
-- migrations/053_asset_tasks_gc_tables.sql
-- GC asset_tasks pipeline: new raw + staging + analytics objects.
-- Parallel to the Ontel asset_tasks pipeline but covering ~294 non-Ontel orgs.
--
-- ADDITIVE ONLY. No existing tables, MVs, or RPCs are touched.
-- Safe to apply during business hours.
--
-- Spec: docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md

BEGIN;

-- 1. raw table (single, unpartitioned — see spec §6 for rationale)
CREATE TABLE data_raw.raw_asset_tasks_gc (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id      UUID NOT NULL,
    org_did     TEXT NOT NULL,
    project_did TEXT NOT NULL,
    data        JSONB NOT NULL
);

-- run_id index: dropped/recreated around bulk load (matches Ontel pattern)
CREATE INDEX idx_raw_asset_tasks_gc_run_id
    ON data_raw.raw_asset_tasks_gc (run_id);

-- loaded_at index: dropped/recreated around bulk load
CREATE INDEX idx_raw_asset_tasks_gc_loaded_at
    ON data_raw.raw_asset_tasks_gc (loaded_at DESC);

-- org_did index: STAYS UP across bulk loads (used only for cleanup
-- DELETE/COUNT, never on insert hot path). Composite with run_id makes
-- per-org cleanup an index scan.
CREATE INDEX idx_raw_asset_tasks_gc_org_did_run_id
    ON data_raw.raw_asset_tasks_gc (org_did, run_id);

-- 2. stg_asset_tasks_gc — mirror of stg_asset_tasks columns
CREATE TABLE data_staging.stg_asset_tasks_gc (LIKE data_staging.stg_asset_tasks INCLUDING ALL);

-- 3. stg_assets_gc — mirror of stg_assets columns
CREATE TABLE data_staging.stg_assets_gc (LIKE data_staging.stg_assets INCLUDING ALL);

-- 4. RPC: aggregate_assets_gc — clone of data_raw.aggregate_assets_from_raw
--    but reads raw_asset_tasks_gc and writes stg_assets_gc.
--    PASTE the body of data_raw.aggregate_assets_from_raw HERE with these
--    substitutions: raw_asset_tasks -> raw_asset_tasks_gc,
--                   stg_assets -> stg_assets_gc.
--    Function signature must match the Ontel one (returns BIGINT or void
--    depending on the original — check migration 014 before pasting).
CREATE OR REPLACE FUNCTION data_staging.aggregate_assets_gc(p_run_id text)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
-- BODY COPIED FROM 014_aggregate_assets_rpc.sql with table-name substitutions
-- (see Step 4 verification below)
BEGIN
  -- placeholder; actual body filled in Step 4
  RAISE EXCEPTION 'aggregate_assets_gc body not implemented yet';
END;
$$;

-- 5. RPC: transform_asset_tasks_gc — clone of data_staging.transform_asset_tasks
--    PASTE the body of data_staging.transform_asset_tasks HERE with these
--    substitutions: raw_asset_tasks -> raw_asset_tasks_gc,
--                   stg_asset_tasks -> stg_asset_tasks_gc.
CREATE OR REPLACE FUNCTION data_staging.transform_asset_tasks_gc(p_run_id text)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
-- BODY COPIED FROM the migration that defines transform_asset_tasks
BEGIN
  RAISE EXCEPTION 'transform_asset_tasks_gc body not implemented yet';
END;
$$;

-- 6. mv_project_summary_gc — clone of mv_project_summary using _gc sources
--    PASTE the SELECT from migration 021 line 170 with these substitutions:
--      stg_asset_tasks -> stg_asset_tasks_gc
--      stg_assets -> stg_assets_gc
CREATE MATERIALIZED VIEW analytics.mv_project_summary_gc AS
-- BODY COPIED FROM 021_analytics_schema.sql with substitutions
SELECT NULL::text AS placeholder WHERE FALSE;

-- 7. mv_technician_stats_gc — clone of mv_technician_stats
CREATE MATERIALIZED VIEW analytics.mv_technician_stats_gc AS
-- BODY COPIED FROM 021_analytics_schema.sql with substitutions
SELECT NULL::text AS placeholder WHERE FALSE;

-- 8. mv_daily_completion_gc — clone of mv_daily_completion
CREATE MATERIALIZED VIEW analytics.mv_daily_completion_gc AS
-- BODY COPIED FROM 021_analytics_schema.sql with substitutions
SELECT NULL::text AS placeholder WHERE FALSE;

-- 9. Indexes on MVs — mirror whatever the Ontel MVs have (check pg_indexes
--    for 'mv_project_summary' first, then create the _gc parallels)

COMMIT;
```

**IMPORTANT:** The placeholder `BEGIN; RAISE EXCEPTION ...` and `SELECT NULL::text WHERE FALSE` are intentional. **You must fill them in by reading the original definitions** before applying. Each substitution is mechanical: replace `raw_asset_tasks` with `raw_asset_tasks_gc`, `stg_asset_tasks` with `stg_asset_tasks_gc`, `stg_assets` with `stg_assets_gc`. Do not introduce any other change.

- [ ] **Step 4: Fill in the RPC and MV bodies**

For each placeholder block in `053_asset_tasks_gc_tables.sql`:
1. Open the source migration file (`014_aggregate_assets_rpc.sql`, `021_analytics_schema.sql`, etc.)
2. Copy the SELECT or function body verbatim
3. Apply ONLY the three table-name substitutions listed above
4. Replace the placeholder in `053_*.sql`

Verify each substitution by `grep` on the resulting `053_*.sql`:
```bash
grep -E "raw_asset_tasks[^_]|stg_asset_tasks[^_]|stg_assets[^_]" swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql
```
Expected: zero matches (every reference should be a `_gc` variant after substitution).

- [ ] **Step 5: Write `apply_053.py` mirroring `apply_052.py`**

Create `swift_api_pipeline/migrations/apply_053.py`:

```python
"""Apply migration 053: GC asset_tasks tables, RPCs, and MVs.

Purely additive — no existing objects are touched. Safe during business hours.
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)


async def main():
    import asyncpg

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_DB_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
        user=os.getenv("SUPABASE_DB_USER", "postgres"),
        password=os.getenv("SUPABASE_PASSWORD"),
        database="postgres",
        ssl=ctx,
        server_settings={"statement_timeout": "0"},
    )

    sql_path = Path(__file__).with_name("053_asset_tasks_gc_tables.sql")
    sql = sql_path.read_text(encoding="utf-8")

    print("Migration 053: GC asset_tasks tables, RPCs, and MVs")
    print("=" * 70)

    # Pre-flight: confirm the _gc objects don't exist yet
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname='data_raw' AND c.relname='raw_asset_tasks_gc')"
    )
    if exists:
        print("ERROR: raw_asset_tasks_gc already exists. Migration already applied?")
        sys.exit(1)

    print("Pre-flight: clean state (no _gc objects exist)")
    print()
    print("Applying migration...")
    await conn.execute(sql)
    print("Migration applied successfully.")

    # Post-verify
    print()
    print("Post-verify:")
    checks = [
        ("data_raw.raw_asset_tasks_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_raw' AND c.relname='raw_asset_tasks_gc')"),
        ("data_staging.stg_asset_tasks_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_staging' AND c.relname='stg_asset_tasks_gc')"),
        ("data_staging.stg_assets_gc table",
         "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
         "WHERE n.nspname='data_staging' AND c.relname='stg_assets_gc')"),
        ("data_staging.aggregate_assets_gc function",
         "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid "
         "WHERE n.nspname='data_staging' AND p.proname='aggregate_assets_gc')"),
        ("data_staging.transform_asset_tasks_gc function",
         "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid "
         "WHERE n.nspname='data_staging' AND p.proname='transform_asset_tasks_gc')"),
        ("analytics.mv_project_summary_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_project_summary_gc')"),
        ("analytics.mv_technician_stats_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_technician_stats_gc')"),
        ("analytics.mv_daily_completion_gc MV",
         "SELECT EXISTS (SELECT 1 FROM pg_matviews WHERE schemaname='analytics' AND matviewname='mv_daily_completion_gc')"),
    ]
    all_ok = True
    for name, query in checks:
        ok = await conn.fetchval(query)
        marker = "OK" if ok else "MISSING"
        print(f"  {marker:8s} {name}")
        if not ok:
            all_ok = False

    # Confirm 3 indexes on raw_asset_tasks_gc
    idx_count = await conn.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE schemaname='data_raw' AND tablename='raw_asset_tasks_gc'"
    )
    print(f"  {'OK' if idx_count >= 3 else 'MISSING':8s} 3+ indexes on raw_asset_tasks_gc (found {idx_count})")

    await conn.close()

    print()
    print("=" * 70)
    if all_ok and idx_count >= 3:
        print("Migration 053 complete.")
    else:
        print("FAIL: some objects missing.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: Apply the migration**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe -u migrations/apply_053.py
```

Expected: all `OK` lines, no `MISSING`. Runtime: <10 seconds (DDL only, no data movement).

- [ ] **Step 7: Independently verify via MCP**

In a Supabase MCP `execute_sql`:

```sql
-- Confirm Ontel objects are untouched
SELECT relname FROM pg_class c
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE n.nspname = 'data_raw' AND c.relname IN ('raw_asset_tasks', 'raw_asset_tasks_gc')
ORDER BY relname;
```
Expected: both `raw_asset_tasks` (partitioned) and `raw_asset_tasks_gc` (regular) present.

```sql
-- Confirm no rows yet in new tables
SELECT 'raw_asset_tasks_gc' AS tbl, COUNT(*) FROM data_raw.raw_asset_tasks_gc
UNION ALL SELECT 'stg_asset_tasks_gc', COUNT(*) FROM data_staging.stg_asset_tasks_gc
UNION ALL SELECT 'stg_assets_gc', COUNT(*) FROM data_staging.stg_assets_gc;
```
Expected: all zero.

- [ ] **Step 8: Commit**

```bash
git add swift_api_pipeline/migrations/053_asset_tasks_gc_tables.sql
git add -f swift_api_pipeline/migrations/apply_053.py
git commit -m "$(cat <<'EOF'
migration 053: GC asset_tasks tables, RPCs, and MVs (additive)

Creates the new data objects for the GC asset_tasks pipeline parallel
to the existing Ontel one. Purely additive - no existing tables, MVs,
or RPCs touched.

Objects created:
- data_raw.raw_asset_tasks_gc (single unpartitioned table + 3 indexes)
- data_staging.stg_asset_tasks_gc, stg_assets_gc (mirror Ontel staging
  via LIKE INCLUDING ALL)
- data_staging.aggregate_assets_gc(p_run_id) RPC
- data_staging.transform_asset_tasks_gc(p_run_id) RPC
- analytics.mv_project_summary_gc, mv_technician_stats_gc,
  mv_daily_completion_gc materialized views

Applied via apply_053.py (asyncpg, statement_timeout=0). Force-added past
apply_*.py gitignore matching the precedent set by apply_014/024/049/052.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 1.
Spec:  docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md
EOF
)"
```

---

## Task 2: Extract Module — `extract_asset_tasks_gc.py`

**Files:**
- Create: `swift_api_pipeline/extract_asset_tasks_gc.py`

This module mirrors `extract_asset_tasks.py` minus all partition-management code. It uses `BaseExtractor` for auth and run tracking, reuses `retry_db`, COPY pattern, and the parallel ThreadPoolExecutor.

- [ ] **Step 1: Read `extract_asset_tasks.py` end-to-end to identify reusable vs partition-specific code**

```bash
wc -l swift_api_pipeline/extract_asset_tasks.py
```

Read the whole file. Identify:
- `get_project_dids()` → replace with `get_gc_projects()`
- `ensure_partitions_exist()`, `_partition_suffix()`, `_get_partition_names()` → **drop entirely**
- `_PARTITION_INDEX_SUFFIXES`, `_INDEXES_TO_DROP_ONLY` → replace with `_INDEXES` (the two write-path indexes)
- `prepare_table_for_bulk_load()` → simpler: drop two global indexes
- `restore_table_after_load()` → recreate two global indexes
- `clear_old_raw_data(projects, project_rows, failed_projects)` → rewrite as per-org with batched DELETE
- `extract_and_load_project()` → adapt to take `org_did` and include it in the COPY columns
- `run_asset_task_pipeline()` → rename to `run_asset_task_gc_pipeline()` with org-level orchestration

- [ ] **Step 2: Write the new module**

Create `swift_api_pipeline/extract_asset_tasks_gc.py`:

```python
#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for all GC (non-Ontel) projects.

Architecture: 12 extraction workers each write directly to DB after every
API page. Single unpartitioned data_raw.raw_asset_tasks_gc table (see spec
docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md §6
for rationale). Per-org safety check + batched cleanup DELETE.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict

import requests

from base_extractor import BaseExtractor
from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE, get_logger, retry_db
)

logger = get_logger("asset_tasks_gc")

# Tunables
PAGE_SIZE = 1000
MAX_WORKERS = 12  # 2x Ontel because GC has 150x the project count
PROJECT_TIMEOUT_SECONDS = 600
MAX_RETRIES = 10
RETRY_WAIT_SECONDS = 120

# Write-path indexes (dropped before bulk load, recreated after).
# org_did index stays up — used only for cleanup, not insert hot path.
_INDEXES = [
    ("idx_raw_asset_tasks_gc_run_id",
     "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_gc_run_id ON data_raw.raw_asset_tasks_gc (run_id)"),
    ("idx_raw_asset_tasks_gc_loaded_at",
     "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_gc_loaded_at ON data_raw.raw_asset_tasks_gc (loaded_at DESC)"),
]


class AssetTaskGCExtractor(BaseExtractor):
    CLEANUP_ROW_THRESHOLD = 0.90  # per-org safety check

    def __init__(self):
        super().__init__(pipeline_name="asset_tasks_gc_extract")

    def get_gc_projects(self) -> List[Dict]:
        """All active GC projects from stg_projects (auto-discovered)."""
        rows = self.db.fetch(
            f"SELECT org_did, org_name, project_did, project_name, status "
            f"FROM {SCHEMA_STAGING}.stg_projects "
            f"WHERE org_name != 'Ontel' "
            f"AND org_name NOT LIKE 'Testing%' "
            f"ORDER BY org_name, project_name"
        )
        return [dict(r) for r in rows]

    def prepare_table_for_bulk_load(self):
        """Drop write-path indexes for fast bulk loading. org_did stays."""
        logger.info("Preparing raw_asset_tasks_gc for bulk load (drop indexes)...")
        for idx_name, _ in _INDEXES:
            self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        logger.info("Indexes dropped (org_did stays up)")

    def restore_table_after_load(self):
        """Recreate write-path indexes after bulk load. ~30-45s each on ~2M rows."""
        logger.info("Restoring raw_asset_tasks_gc write-path indexes...")
        for idx_name, idx_def in _INDEXES:
            logger.info(f"  Creating {idx_name}...")
            self.db.execute(idx_def)
        logger.info("Indexes restored")

    def clear_old_raw_data(self, successful_org_dids: list):
        """Single batched DELETE for all orgs that passed extraction.

        Failed orgs (not in successful_org_dids) retain their prior data,
        same fallback behavior as Ontel's partial-success path.
        """
        if not successful_org_dids:
            logger.warning("No successful orgs — skipping cleanup entirely")
            return

        run_id_str = str(self.run_id)
        logger.info(f"Cleanup: deleting old rows for {len(successful_org_dids)} orgs...")
        retry_db(
            lambda: self.db.execute(
                f'DELETE FROM {SCHEMA_RAW}.raw_asset_tasks_gc '
                f'WHERE org_did = ANY($1) AND run_id != $2',
                successful_org_dids, run_id_str
            ),
            description=f"batched cleanup of {len(successful_org_dids)} orgs"
        )
        logger.info("Cleanup complete")

    def per_org_safety_check(self, org_rows: dict) -> List[str]:
        """For each org with > 0 new rows, verify new_count >= 90% of old_count.

        Returns list of org_dids that passed the check and should be cleaned up.
        """
        run_id_str = str(self.run_id)
        successful = []
        for org_did, new_count in org_rows.items():
            if new_count == 0:
                logger.warning(f"[org={org_did}] Skipped cleanup (0 rows extracted)")
                continue
            old_count = self.db.fetchval(
                f'SELECT COUNT(*) FROM {SCHEMA_RAW}.raw_asset_tasks_gc '
                f'WHERE org_did = $1 AND run_id != $2',
                org_did, run_id_str
            ) or 0
            if old_count > 0 and new_count < old_count * self.CLEANUP_ROW_THRESHOLD:
                logger.warning(
                    f"[org={org_did}] Cleanup skipped: new={new_count:,}, "
                    f"old={old_count:,} (below {self.CLEANUP_ROW_THRESHOLD:.0%} threshold)."
                )
                continue
            successful.append(org_did)
        return successful

    def extract_and_load_project(self, org_did: str, project_did: str,
                                  project_name: str) -> int:
        """Extract one project's asset_tasks and COPY into raw_asset_tasks_gc."""
        run_id_str = str(self.run_id)
        url = f"https://prod.api.swiftprojects.io/api/next/projects/{project_did}/assets/_export"
        params = {"pageSize": PAGE_SIZE, "dateFormat": "yyyy-MM-dd", "timezone": "America/New_York"}
        headers = self.get_auth_headers()
        project_rows = 0
        page_count = 0
        after_ap = None
        after_id = None
        start = time.monotonic()

        while True:
            if time.monotonic() - start > PROJECT_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"[{project_name}] Exceeded {PROJECT_TIMEOUT_SECONDS}s timeout "
                    f"after {project_rows:,} rows"
                )
            if after_ap and after_id:
                params['afterAp'] = after_ap
                params['afterId'] = after_id

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)
                    if resp.status_code == 204:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        return project_rows
                    resp.raise_for_status()
                    body = resp.json()
                    rows = body.get("list", [])
                    if not rows:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        return project_rows

                    # COPY into raw_asset_tasks_gc with org_did column
                    tuples = [(run_id_str, org_did, project_did, r) for r in rows]
                    retry_db(
                        lambda: self.db.copy_records_to_table(
                            "raw_asset_tasks_gc",
                            schema_name=SCHEMA_RAW,
                            records=tuples,
                            columns=["run_id", "org_did", "project_did", "data"],
                        ),
                        description=f"copy {len(rows)} rows for {project_name}"
                    )
                    project_rows += len(rows)
                    page_count += 1
                    if page_count % 25 == 0:
                        logger.info(f"[{project_name}] Page {page_count} - {project_rows:,} rows")

                    next_info = body.get("next")
                    if not next_info:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        return project_rows
                    after_ap = next_info.get("ap")
                    after_id = next_info.get("id")
                    break

                except requests.HTTPError as e:
                    if attempt == MAX_RETRIES - 1:
                        raise
                    logger.error(f"[{project_name}] Retry {attempt+1}/{MAX_RETRIES}: {e}")
                    time.sleep(2 ** attempt)
                    headers = self.get_auth_headers()
            else:
                raise RuntimeError(f"[{project_name}] Failed after {MAX_RETRIES} retries")


def run_asset_task_gc_pipeline():
    """Full GC asset_tasks pipeline: extract + inline transforms."""
    extractor = AssetTaskGCExtractor()
    extractor.start_pipeline_run()
    try:
        extractor.authenticate()
        projects = extractor.get_gc_projects()
        logger.info(f"Found {len(projects)} GC projects across "
                    f"{len(set(p['org_did'] for p in projects))} orgs")

        extractor.prepare_table_for_bulk_load()

        # Parallel extract
        org_rows = {}  # org_did -> total new rows across that org's projects
        failed_projects = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    extractor.extract_and_load_project,
                    p["org_did"], p["project_did"], p["project_name"]
                ): p for p in projects
            }
            for future in as_completed(futures, timeout=PROJECT_TIMEOUT_SECONDS + 300):
                p = futures[future]
                try:
                    rows = future.result()
                    org_rows[p["org_did"]] = org_rows.get(p["org_did"], 0) + rows
                except Exception as e:
                    logger.error(f"[{p['project_name']}] FAILED: {type(e).__name__}: {e}")
                    failed_projects.append(p["project_name"])

        total_records = sum(org_rows.values())
        extractor.total_loaded = total_records

        extractor.restore_table_after_load()

        # Per-org safety check + batched cleanup
        successful_orgs = extractor.per_org_safety_check(org_rows)
        extractor.clear_old_raw_data(successful_orgs)

        # Partial-success: same pattern as Ontel (status='success', detail in error_message)
        if failed_projects:
            error_detail = (
                f"Partial extraction: {len(projects) - len(failed_projects)}/{len(projects)} "
                f"projects succeeded. Failed: {', '.join(failed_projects[:10])}"
                f"{'...' if len(failed_projects) > 10 else ''}"
            )
            extractor.complete_pipeline_run("success", total_records, error=error_detail)
            logger.warning(f"GC pipeline PARTIAL SUCCESS: {error_detail}")
        else:
            extractor.complete_pipeline_run("success", total_records)
            logger.info(f"GC pipeline completed: {total_records:,} rows across "
                        f"{len(org_rows)} orgs")

        # Inline transforms
        from transform import run_assets_gc_transform, run_asset_tasks_gc_transform
        run_assets_gc_transform(str(extractor.run_id))
        run_asset_tasks_gc_transform(str(extractor.run_id))

        return str(extractor.run_id)

    except Exception as e:
        logger.error(f"GC pipeline failed: {e}")
        try:
            extractor.restore_table_after_load()
        except Exception:
            pass
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    run_asset_task_gc_pipeline()
```

- [ ] **Step 3: Quick syntax check**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe -c "import ast; ast.parse(open('extract_asset_tasks_gc.py', encoding='utf-8').read()); print('SYNTAX OK')"
```
Expected: `SYNTAX OK`

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks_gc.py
git commit -m "feat(asset_tasks_gc): new extractor module for non-Ontel GC orgs

Mirrors extract_asset_tasks.py architecture (BaseExtractor + parallel
ThreadPoolExecutor + COPY-per-page + per-X safety check) but drops all
partition-management code in favor of the single unpartitioned design.

Key differences from Ontel:
- get_gc_projects() auto-discovers from stg_projects with the spec filter
  (org_name != 'Ontel' AND org_name NOT LIKE 'Testing%')
- 12 workers (2x Ontel) for the much larger project count (~1,063 projects)
- COPY columns include org_did (Ontel doesn't carry org_did since it's
  implicit Ontel-only)
- prepare/restore_table_after_load drops only the two write-path indexes
  (run_id, loaded_at); the (org_did, run_id) composite stays up since it's
  cleanup-only, never on the insert hot path
- clear_old_raw_data is one batched DELETE WHERE org_did = ANY($1)
  AND run_id != $2 (single round-trip, not 1,063 per-project ones)
- Per-org 90% safety check (Ontel's was per-project)

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 2."
```

---

## Task 3: Transform Glue + Notifier Wiring

**Files:**
- Modify: `swift_api_pipeline/transform.py`
- Modify: `swift_api_pipeline/pipeline_notifier.py`

Add two new transform functions that call the `_gc` RPCs created in migration 053, and wire up PIPELINE_TABLES entries so notification emails show row-count diffs.

- [ ] **Step 1: Find the existing Ontel transforms to mirror**

```bash
grep -n "^def run_assets_transform\|^def run_asset_tasks_transform" swift_api_pipeline/transform.py
```

Note the line numbers; read the bodies of both functions to understand the pattern (look up latest successful extract run_id if None, call DELETE on staging, call the aggregation RPC, validate counts).

- [ ] **Step 2: Add `run_assets_gc_transform()` right after `run_assets_transform()` in `transform.py`**

Paste this immediately after the existing `run_assets_transform()` function (matching the existing function's style):

```python
def run_assets_gc_transform(run_id: str = None):
    """Run GC assets transformation only.

    Aggregates from raw_asset_tasks_gc into stg_assets_gc via the
    aggregate_assets_gc RPC. Mirror of run_assets_transform.
    """
    print(f"\n{'='*60}")
    print(f"GC Assets Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_gc_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks_gc_extract run_id: {run_id}")
        else:
            print("No successful asset_tasks_gc_extract runs found")
            return

    run_id = str(run_id)
    asset_count = db.fetchval(
        f"SELECT {SCHEMA_STAGING}.aggregate_assets_gc($1)", run_id
    )

    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_assets_gc')
    print(f"\nRow Count Validation:")
    print(f"  [stg_assets_gc]: transformed={asset_count:,} | staging={stg_count:,}")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  GC Assets: {asset_count:,}")
    print(f"{'='*60}\n")
```

- [ ] **Step 3: Add `run_asset_tasks_gc_transform()` right after `run_asset_tasks_transform()` in `transform.py`**

```python
def run_asset_tasks_gc_transform(run_id: str = None):
    """Run GC asset tasks transformation only.

    Aggregates from raw_asset_tasks_gc into stg_asset_tasks_gc via the
    transform_asset_tasks_gc RPC. Mirror of run_asset_tasks_transform.
    """
    print(f"\n{'='*60}")
    print(f"GC Asset Tasks Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_gc_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks_gc_extract run_id: {run_id}")
        else:
            print("No successful asset_tasks_gc_extract runs found")
            return

    asset_count = db.fetchval(
        f"SELECT {SCHEMA_STAGING}.transform_asset_tasks_gc($1)", run_id
    )

    print(f"\nRow Count Validation:")
    raw_count = db.fetchval(
        f'SELECT COUNT(*) FROM {SCHEMA_RAW}.raw_asset_tasks_gc WHERE run_id = $1',
        run_id
    )
    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_asset_tasks_gc')
    status = "OK" if raw_count == stg_count else "MISMATCH"
    print(f"  [stg_asset_tasks_gc]: raw={raw_count:,} | transformed={asset_count:,} "
          f"| staging={stg_count:,} [{status}]")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  GC Asset Tasks: {asset_count:,}")
    print(f"{'='*60}\n")
```

- [ ] **Step 4: Add a `refresh_analytics_gc()` helper near the existing `refresh_analytics()`**

```python
def refresh_analytics_gc():
    """Refresh only the _gc materialized views.

    Calls refresh_one_mv() on each of the three GC MVs. Mirror of
    refresh_analytics() but scoped to the _gc set so the Ontel MVs
    are unaffected.
    """
    print(f"\n{'='*60}")
    print(f"GC Analytics MV Refresh")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()
    for mv in ("mv_project_summary_gc", "mv_technician_stats_gc", "mv_daily_completion_gc"):
        start = datetime.now()
        db.execute(f"SELECT analytics.refresh_one_mv($1)", mv)
        ms = int((datetime.now() - start).total_seconds() * 1000)
        print(f"  {mv}: {ms:,}ms")

    print(f"\n{'='*60}\n")
```

- [ ] **Step 5: Add `PIPELINE_TABLES` entries to `pipeline_notifier.py`**

Find the `PIPELINE_TABLES` dict and add these four entries (preserve alphabetical ordering if it exists):

```python
    "Asset Tasks GC": [
        ("data_raw", "raw_asset_tasks_gc"),
        ("data_staging", "stg_assets_gc"),
        ("data_staging", "stg_asset_tasks_gc"),
    ],
    "Asset Tasks GC Extract": [
        ("data_raw", "raw_asset_tasks_gc"),
    ],
    "Asset Tasks GC Transform": [
        ("data_staging", "stg_assets_gc"),
        ("data_staging", "stg_asset_tasks_gc"),
    ],
    "Analytics GC MV Refresh": [],
```

- [ ] **Step 6: Syntax check**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe -c "import ast; ast.parse(open('transform.py', encoding='utf-8').read()); ast.parse(open('pipeline_notifier.py', encoding='utf-8').read()); print('SYNTAX OK')"
```

- [ ] **Step 7: Commit**

```bash
git add swift_api_pipeline/transform.py swift_api_pipeline/pipeline_notifier.py
git commit -m "feat(asset_tasks_gc): transform glue + notifier wiring

Adds three new functions in transform.py:
- run_assets_gc_transform(run_id=None) -> calls aggregate_assets_gc RPC
- run_asset_tasks_gc_transform(run_id=None) -> calls transform_asset_tasks_gc RPC
- refresh_analytics_gc() -> refreshes only the three _gc MVs

Each mirrors its Ontel counterpart (auto-lookup of latest successful
extract run_id, DELETE+INSERT pattern via RPC, row-count validation).

PIPELINE_TABLES gets four new entries so notification emails render the
before/after row-count comparison for the new pipeline names.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 3."
```

---

## Task 4: Main.py Argparse + Pipeline Wrappers

**Files:**
- Modify: `swift_api_pipeline/main.py`

Add the new `--pipeline` choices and wire them to the functions from Tasks 2 and 3.

- [ ] **Step 1: Add four new entries to `PIPELINE_NAMES` dict**

Find `PIPELINE_NAMES = {` in `main.py` and add (preserving existing order — these go right after the Ontel `asset_tasks_*` entries):

```python
    "asset_tasks_gc": "Asset Tasks GC",
    "asset_tasks_gc_extract": "Asset Tasks GC Extract",
    "asset_tasks_gc_transform": "Asset Tasks GC Transform",
    "analytics_gc": "Analytics GC MV Refresh",
```

- [ ] **Step 2: Add three wrapper functions**

After `run_asset_tasks_transform_pipeline()` (or wherever the Ontel `asset_tasks_*` wrappers live), add:

```python
def run_asset_tasks_gc_pipeline():
    """Run GC asset_tasks combined extract + transforms.

    Calls run_asset_task_gc_pipeline() which performs extract +
    inline run_assets_gc_transform + run_asset_tasks_gc_transform.
    """
    from extract_asset_tasks_gc import run_asset_task_gc_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC PIPELINE")
    logger.info(f"{'#'*60}")

    run_asset_task_gc_pipeline()
    return True


def run_asset_tasks_gc_extract_pipeline():
    """Run GC asset_tasks EXTRACT only (Swift API -> raw_asset_tasks_gc)."""
    from extract_asset_tasks_gc import AssetTaskGCExtractor
    from extract_asset_tasks_gc import run_asset_task_gc_pipeline

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC EXTRACT")
    logger.info(f"{'#'*60}")

    # Reuse the full-pipeline function but skip the inline transforms.
    # Simplest implementation: extract-only flag via env var, OR factor out.
    # For v1, the simplest path: call run_asset_task_gc_pipeline() which
    # also runs transforms — extract_only is YAGNI for now.
    run_asset_task_gc_pipeline()
    return True


def run_asset_tasks_gc_transform_pipeline():
    """Run GC asset_tasks TRANSFORM only.

    Looks up the latest successful asset_tasks_gc_extract run_id from
    pipeline.pipeline_runs and runs the SQL aggregation RPCs only.
    """
    from transform import run_assets_gc_transform, run_asset_tasks_gc_transform

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ASSET TASKS GC TRANSFORM")
    logger.info(f"{'#'*60}")

    run_assets_gc_transform()
    run_asset_tasks_gc_transform()
    return True


def run_analytics_gc_refresh():
    """Refresh the three _gc MVs only."""
    from transform import refresh_analytics_gc

    logger.info(f"\n{'#'*60}")
    logger.info(f"# ANALYTICS GC MV REFRESH")
    logger.info(f"{'#'*60}")

    refresh_analytics_gc()
    return True
```

**Note on the extract_only wrapper:** for simplicity, `run_asset_tasks_gc_extract_pipeline()` currently calls the full pipeline. If later you need a true extract-only path, factor `run_asset_task_gc_pipeline` into `_extract()` and `_transform()` halves. YAGNI for v1.

- [ ] **Step 3: Add the four choices to argparse `choices=[...]` and `pipeline_funcs` dict**

Find the argparse `choices=` list and add four entries:

```python
choices=["orgs", "user_priorities", "asset_tasks", "asset_tasks_extract",
         "asset_tasks_transform",
         "asset_tasks_gc", "asset_tasks_gc_extract", "asset_tasks_gc_transform",
         "analytics_gc",
         "forms", "timer", "aging", "sales", "backfill", "analytics", "assets"],
```

Find the `pipeline_funcs = {` dict and add:

```python
    "asset_tasks_gc": run_asset_tasks_gc_pipeline,
    "asset_tasks_gc_extract": run_asset_tasks_gc_extract_pipeline,
    "asset_tasks_gc_transform": run_asset_tasks_gc_transform_pipeline,
    "analytics_gc": run_analytics_gc_refresh,
```

- [ ] **Step 4: Syntax + dispatch check**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe main.py --pipeline asset_tasks_gc --help
```
Expected: argparse accepts the choice and prints help text. No NameError or KeyError.

```bash
./venv/Scripts/python.exe main.py --pipeline asset_tasks_gc_transform --help
./venv/Scripts/python.exe main.py --pipeline analytics_gc --help
```
Expected: all four new choices recognized.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/main.py
git commit -m "feat(asset_tasks_gc): main.py argparse + pipeline wrappers

Adds four new --pipeline choices wired to Task 2/3 functions:
- asset_tasks_gc: full extract + inline transforms
- asset_tasks_gc_extract: extract only (currently aliases full pipeline,
  YAGNI to split until a use case for extract-only emerges)
- asset_tasks_gc_transform: transform only (looks up latest extract run_id)
- analytics_gc: refresh the three _gc MVs only

PIPELINE_NAMES updated so notification emails render the correct
display names; pipeline_funcs dict wires choice -> wrapper.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 4."
```

---

## Task 5: Single-Org Smoke Test

**Files:**
- Create (temp): `swift_api_pipeline/_gc_smoke_test.py` (deleted after smoke passes)

Validate the new code end-to-end against a single small GC org BEFORE wiring the nightly trigger. This catches argparse mistakes, RPC bugs, COPY column mismatches, and validates the per-org safety check works.

- [ ] **Step 1: Pick a small test org**

Via Supabase MCP:
```sql
SELECT org_did, org_name, COUNT(*) AS projects, SUM(asset_task_count) AS tasks
FROM data_staging.stg_projects
WHERE org_name != 'Ontel' AND org_name NOT LIKE 'Testing%'
GROUP BY org_did, org_name
HAVING SUM(asset_task_count) BETWEEN 100 AND 1000
ORDER BY tasks ASC LIMIT 1;
```

Note the `org_did` and `org_name` returned. Use this in Step 2.

- [ ] **Step 2: Write a smoke script that runs the GC extractor scoped to one org**

Create `swift_api_pipeline/_gc_smoke_test.py`:

```python
"""Single-org smoke test for the GC pipeline.

Runs AssetTaskGCExtractor against ONE org (passed as arg) so we can
validate the full extract->transform->MV-refresh chain on a small dataset
before enabling the nightly trigger.

Usage: python -u _gc_smoke_test.py <org_did>
"""
import sys
from extract_asset_tasks_gc import AssetTaskGCExtractor, run_asset_task_gc_pipeline


def main():
    if len(sys.argv) != 2:
        print("Usage: python _gc_smoke_test.py <org_did>")
        sys.exit(1)
    target_org_did = sys.argv[1]

    # Monkey-patch get_gc_projects to return ONLY the target org's projects.
    original = AssetTaskGCExtractor.get_gc_projects

    def filtered(self):
        all_projects = original(self)
        kept = [p for p in all_projects if p["org_did"] == target_org_did]
        if not kept:
            raise RuntimeError(f"No projects found for org_did={target_org_did}")
        print(f"Smoke test: filtering to {len(kept)} projects for org_did={target_org_did}")
        return kept

    AssetTaskGCExtractor.get_gc_projects = filtered

    run_id = run_asset_task_gc_pipeline()
    print(f"\nSmoke test PASSED, run_id={run_id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the smoke test**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe -u _gc_smoke_test.py <ORG_DID_FROM_STEP_1>
```

Watch for:
- `Authenticated successfully`
- `Found N GC projects across 1 orgs`
- `Indexes dropped (org_did stays up)`
- Per-project `Complete - NNN rows` lines
- `GC Assets Transformation` block with row count
- `GC Asset Tasks Transformation` block with `[OK]` validation
- `Smoke test PASSED, run_id=...`

If anything fails, fix it and re-run. Do NOT proceed until this is clean.

- [ ] **Step 4: Verify via MCP that the data landed cleanly**

```sql
-- The smoke run_id should be the only run_id in raw_asset_tasks_gc
SELECT DISTINCT run_id::text FROM data_raw.raw_asset_tasks_gc;
```
Expected: exactly one row (the smoke run_id).

```sql
-- Row counts match across raw/staging
SELECT
  (SELECT COUNT(*) FROM data_raw.raw_asset_tasks_gc) AS raw,
  (SELECT COUNT(*) FROM data_staging.stg_asset_tasks_gc) AS stg_tasks,
  (SELECT COUNT(*) FROM data_staging.stg_assets_gc) AS stg_assets;
```
Expected: raw ~= stg_tasks (within transform aggregation rules), stg_assets > 0.

- [ ] **Step 5: Refresh MVs and verify they populated**

```bash
./venv/Scripts/python.exe main.py --pipeline analytics_gc --no-email
```

Then via MCP:
```sql
SELECT 'mv_project_summary_gc' AS mv, COUNT(*) AS rows FROM analytics.mv_project_summary_gc
UNION ALL SELECT 'mv_technician_stats_gc', COUNT(*) FROM analytics.mv_technician_stats_gc
UNION ALL SELECT 'mv_daily_completion_gc', COUNT(*) FROM analytics.mv_daily_completion_gc;
```
Expected: all three MVs have non-zero rows.

- [ ] **Step 6: Confirm Ontel pipeline tables untouched**

```sql
-- Snapshot Ontel row counts before AND after the smoke; should be identical.
SELECT
  (SELECT COUNT(*) FROM data_raw.raw_asset_tasks) AS ontel_raw,
  (SELECT COUNT(*) FROM data_staging.stg_asset_tasks) AS ontel_stg_tasks,
  (SELECT COUNT(*) FROM data_staging.stg_assets) AS ontel_stg_assets;
```
Expected: same values as before the smoke. If any moved, something is wrong — GC code touched Ontel data.

- [ ] **Step 7: Delete the smoke script (it lives in git history only)**

```bash
rm swift_api_pipeline/_gc_smoke_test.py
```

- [ ] **Step 8: Commit**

```bash
git status  # confirm no tracked files modified, _gc_smoke_test.py is untracked-then-deleted
git commit --allow-empty -m "chore(asset_tasks_gc): single-org smoke test passed

Validated the full extract -> transform -> MV refresh chain against
a single small GC org (org_did=<ORG_DID>) before enabling the nightly
trigger. Row counts matched across raw / staging / analytics MVs.
Ontel pipeline tables untouched (verified via row-count snapshot
before and after the smoke).

The _gc_smoke_test.py helper was monkey-patch-based and not committed.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 5."
```

---

## Task 6: GHA Workflow `pipeline-asset-tasks-gc.yml`

**Files:**
- Create: `.github/workflows/pipeline-asset-tasks-gc.yml`

Clone of `pipeline-asset-tasks.yml` minus all downstream-dispatch steps. Runs the full GC pipeline + analytics refresh.

- [ ] **Step 1: Read the existing Ontel workflow as the template**

```bash
cat .github/workflows/pipeline-asset-tasks.yml
```

Note: trigger types, env, concurrency, secret usage, the inputs-resolution step pattern.

- [ ] **Step 2: Create the GC workflow**

Create `.github/workflows/pipeline-asset-tasks-gc.yml`:

```yaml
name: "Pipeline: Asset Tasks GC"

# Parallel workflow to pipeline-asset-tasks.yml — covers ~294 non-Ontel
# General Contractor orgs. Fires nightly at 02:00 ET via Apps Script
# triggerAssetTasksGC() (well after the Ontel pipeline finishes ~01:00 ET).
#
# Spec: docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md

on:
  repository_dispatch:
    types: [pipeline-asset-tasks-gc]
  workflow_dispatch:

concurrency:
  group: pipeline-asset-tasks-gc
  cancel-in-progress: false

env:
  PIPELINE_DIR: swift_api_pipeline

jobs:
  asset-tasks-gc:
    runs-on: ubuntu-latest
    timeout-minutes: 60

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
          DOTENV
          echo "SWIFT_PASSWORD=${{ secrets.SWIFT_PASSWORD }}" >> .env
          echo "SUPABASE_PASSWORD=${{ secrets.SUPABASE_PASSWORD }}" >> .env

      - name: Decode notifier credentials
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          mkdir -p gmail_credentials
          echo "${{ secrets.NOTIFIER_CREDENTIALS_JSON }}" | base64 -d > gmail_credentials/credentials.json
          echo "${{ secrets.NOTIFIER_TOKEN_PICKLE }}" | base64 -d > gmail_credentials/token.pickle

      - name: Run GC asset_tasks pipeline (extract + transform)
        working-directory: ${{ env.PIPELINE_DIR }}
        run: python -u main.py --pipeline asset_tasks_gc

      - name: Refresh GC analytics MVs
        working-directory: ${{ env.PIPELINE_DIR }}
        run: python -u main.py --pipeline analytics_gc --no-email

      - name: Upload logs on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: asset-tasks-gc-logs
          path: '${{ env.PIPELINE_DIR }}/pipeline_logs/*.log'
          retention-days: 7
```

**Note:** No `Dispatch downstream` step in this workflow — GC has no equivalent of pipeline-asset-tasks-export or date-validator-daily in v1.

- [ ] **Step 3: Validate the YAML parses**

```bash
./venv/Scripts/python.exe -c "
import yaml
y = yaml.safe_load(open(r'.github/workflows/pipeline-asset-tasks-gc.yml', encoding='utf-8'))
print('YAML OK')
print('Triggers:', list(y.get(True, y.get('on', {})).keys()))
print('Jobs:', list(y['jobs'].keys()))
print('Steps:', [s.get('name') for s in y['jobs']['asset-tasks-gc']['steps']])
"
```

Expected: `YAML OK`, triggers include `repository_dispatch` + `workflow_dispatch`, 8 steps listed, NO step named "Dispatch downstream".

- [ ] **Step 4: Commit and push**

```bash
git add .github/workflows/pipeline-asset-tasks-gc.yml
git commit -m "feat(asset_tasks_gc): GHA workflow for nightly GC pipeline

Clone of pipeline-asset-tasks.yml with three key differences:
- event_type 'pipeline-asset-tasks-gc' + own concurrency group
- 60-min timeout (vs Ontel's 90 min; GC runs faster at ~20 min total)
- NO downstream dispatches (no export, no validator for GC in v1)

Apps Script trigger (Task 7) fires this nightly at 02:00 AM ET.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 6."
git push origin main
```

The push is required so GHA recognizes the workflow file before Task 7's Apps Script test triggers it.

- [ ] **Step 5: Manual test via workflow_dispatch**

In the GitHub UI: **Actions → Pipeline: Asset Tasks GC → Run workflow → Run workflow** (no inputs needed).

Watch the run. Expected total runtime: ~20 min for the full ~2M-row extract + transforms + MV refresh.

Watch for:
- All 8 steps green
- No "Upload logs on failure" artifact (it only fires on failure)
- `pipeline.pipeline_runs` shows a new `asset_tasks_gc_extract` row with `status='success'` and ~2M records_extracted

If the run fails, download the logs artifact and debug. Do not proceed to Task 7 until this passes.

---

## Task 7: Apps Script Trigger

**Files:**
- Modify: `scripts/pipeline_trigger.gs`

Add a function that fires the GC workflow daily via `repository_dispatch`.

- [ ] **Step 1: Add `triggerAssetTasksGC()` to `scripts/pipeline_trigger.gs`**

Append after the existing `triggerCalendarLeave()` function:

```javascript
/**
 * Trigger GC asset_tasks pipeline.
 * Schedule daily at 02:00 AM EST — well after the Ontel pipeline finishes
 * (~01:00 ET post-Task-6) so we avoid Swift API rate-limit collisions and
 * DB pool contention.
 *
 * GC pipeline writes to separate _gc tables (raw_asset_tasks_gc,
 * stg_asset_tasks_gc, etc.) and refreshes its own MVs. No downstream
 * dispatches in v1 — no export or validator emails.
 */
function triggerAssetTasksGC() {
  fireDispatch_('pipeline-asset-tasks-gc');
}
```

Also update the file-header schedule comment to document the new trigger time:

```javascript
 * Schedules (EST):
 *   10:13 PM  — Orgs & Projects
 *   12:09 AM  — Timer
 *   12:13 AM  — User Priorities
 *   12:17 AM  — QA Forms
 *   01:30 AM  — Asset Tasks (shakedown phase, see triggerAssetTasks)
 *   02:00 AM  — Asset Tasks GC  <-- new
```

- [ ] **Step 2: Commit and push**

```bash
git add scripts/pipeline_trigger.gs
git commit -m "feat(asset_tasks_gc): Apps Script trigger for GC nightly pipeline

triggerAssetTasksGC() fires repository_dispatch 'pipeline-asset-tasks-gc'
once a night. Reuses the existing fireDispatch_ helper and GITHUB_TOKEN
script property from gmail_trigger.gs.

User installs this in the existing nanoninth.com Apps Script project
alongside the other pipeline triggers; time-driven trigger at 02:00 AM EST.

Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md Task 7."
git push origin main
```

- [ ] **Step 3: USER ACTION — install in Apps Script**

This is a manual step (Apps Script project lives in Google's UI, not in git):

1. Open the existing Apps Script project under `jamil.mendez@nanoninth.com` (same one that has `triggerOrgs`, `triggerLightPipelines`, etc.)
2. In `pipeline_trigger.gs` (or whatever name you used there), paste the new `triggerAssetTasksGC` function from the committed file
3. Save (Ctrl+S)
4. Triggers (clock icon) → Add Trigger:
   - Function: `triggerAssetTasksGC`
   - Event source: Time-driven
   - Type: Day timer
   - Time of day: **2am to 3am**
   - Failure notification: Notify me immediately
5. Save the trigger (accept OAuth permissions if prompted)

- [ ] **Step 4: USER ACTION — manual trigger test**

In Apps Script: select `triggerAssetTasksGC` → Run. Watch the execution log for:
```
Dispatched pipeline-asset-tasks-gc successfully
```

Then check GitHub Actions: a fresh `Pipeline: Asset Tasks GC` run should start within 10 seconds. Let it complete (~20 min). Confirm `pipeline_runs.status='success'` for `asset_tasks_gc_extract` afterwards.

---

## Task 8: End-to-End Production Validation

**Files:** none (validation only)

After the time-driven trigger is installed, the GC pipeline runs nightly at 02:00 ET. Validate the first 2-3 nights by hand, then schedule recurring verification.

- [ ] **Step 1: Wait for the first scheduled run**

The morning after Task 7, check:
1. **GitHub Actions** → `Pipeline: Asset Tasks GC` shows a green run that started around 02:00 ET
2. The run duration is ~15-25 min (within the 60-min timeout)

- [ ] **Step 2: Verify the data via Supabase MCP**

```sql
-- Latest GC extract run
SELECT run_id::text, status,
  TO_CHAR(started_at AT TIME ZONE 'America/New_York', 'YYYY-MM-DD HH24:MI:SS') AS started_et,
  TO_CHAR(completed_at AT TIME ZONE 'America/New_York', 'HH24:MI:SS') AS completed_et,
  records_extracted,
  EXTRACT(EPOCH FROM (completed_at - started_at))::int AS dur_sec,
  LEFT(COALESCE(error_message,''), 100) AS err
FROM pipeline.pipeline_runs
WHERE pipeline_name = 'asset_tasks_gc_extract'
ORDER BY started_at DESC LIMIT 3;
```

Expected:
- Latest row: `status='success'`, `records_extracted` ~= 1.9-2.1M, `dur_sec` < 1500 (25 min)
- `error_message` empty OR contains "Partial extraction: N/M projects succeeded..." (acceptable partial-success)

```sql
-- Distinct orgs and per-org row distribution
SELECT
  COUNT(DISTINCT org_did) AS distinct_orgs,
  COUNT(*) AS total_rows,
  (SELECT COUNT(*) FROM data_staging.stg_asset_tasks_gc) AS stg_rows,
  (SELECT COUNT(*) FROM data_staging.stg_assets_gc) AS stg_assets_rows
FROM data_raw.raw_asset_tasks_gc;
```

Expected: ~294 distinct_orgs, total_rows ~= stg_rows (within transform aggregation rules), stg_assets_rows in the tens of thousands.

```sql
-- MV freshness
SELECT relname,
  EXTRACT(EPOCH FROM (NOW() - GREATEST(last_vacuum, last_analyze)))::int AS sec_since
FROM pg_stat_all_tables
WHERE schemaname='analytics' AND relname LIKE 'mv_%_gc'
ORDER BY relname;
```

Expected: all three `_gc` MVs `sec_since` < 14400 (4 hours), confirming recent refresh.

- [ ] **Step 3: Confirm Ontel pipeline still healthy**

Same morning check the Ontel runs:

```sql
SELECT pipeline_name, status,
  TO_CHAR(completed_at AT TIME ZONE 'America/New_York', 'HH24:MI:SS') AS completed_et,
  records_extracted
FROM pipeline.pipeline_runs
WHERE started_at >= (NOW() AT TIME ZONE 'America/New_York')::date - INTERVAL '1 day'
  AND pipeline_name IN ('asset_tasks_extract', 'asset_tasks_gc_extract')
ORDER BY started_at DESC LIMIT 5;
```

Expected: both pipelines `status='success'`, Ontel finished hours before GC started (no overlap), Ontel row count unchanged from baseline.

- [ ] **Step 4: Schedule a recurring verification (optional)**

If you want a daily 03:00 ET sanity-check report (same pattern as Ontel had during its shakedown), use the `/schedule` skill to create a routine that runs the SQL from Steps 2 and 3 and posts an OK/ALERT summary. The routine is one-time-per-day; pause after ~5 nights of green or once Task 8 is otherwise marked done.

- [ ] **Step 5: Mark plan complete in project memory + WORK_LOG**

Update `memory/project_asset_tasks_gc_pipeline.md` (create if missing) to capture:
- Plan path
- Migration number (053)
- Tables/MVs created
- Trigger time (02:00 ET)
- Any tuning knobs we ended up adjusting (MAX_WORKERS, etc.)

Update `WORK_LOG.md` with a final entry under the existing Session 11 (or start Session 12) capturing the GC pipeline shipping.

```bash
git add memory/project_asset_tasks_gc_pipeline.md WORK_LOG.md
git commit -m "docs: GC asset_tasks pipeline complete + WORK_LOG entry"
git push origin main
```

---

## Plan complete

After Task 8 passes, the GC asset_tasks pipeline is in production. It runs nightly at 02:00 ET, writes to `_gc`-suffixed tables, refreshes its own MVs, and operates fully independently of the Ontel pipeline.
