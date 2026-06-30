# Asset Tasks Pipeline — GHA Migration with Partitioned Tables

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the asset task pipeline from the local Windows PC to GitHub Actions, using PostgreSQL table partitioning so each TS project writes to its own partition. New projects are auto-detected and auto-partitioned. The pipeline runs as a single GHA workflow with internal parallelism (~60 min, ~1,800 GHA min/month).

**Architecture:** Convert `raw_asset_tasks` from a regular table to a `PARTITION BY LIST (project_did)` table. Each TS project gets its own partition (e.g., `raw_asset_tasks_ts13`). The extraction code auto-creates missing partitions before extraction starts. The safety check (90% row threshold) becomes per-project instead of all-or-nothing. On failure, only the failed project's partition retains stale data — successful projects keep their fresh data. A new GHA workflow (`pipeline-asset-tasks.yml`) runs the full pipeline nightly on a cron schedule, with the existing `scheduled_main_pipeline.bat` updated to skip asset_tasks (it becomes GHA dispatch + local fallback only).

**Tech Stack:** PostgreSQL 15 (Supabase), Python 3.14, asyncpg, GitHub Actions (ubuntu-latest)

**Key Constraints:**
- GHA uses the Supabase connection pooler (`aws-0-ap-southeast-1.pooler.supabase.com`), which has a ~5-6 min hard connection limit
- Index rebuilds must complete within that window — partitioned indexes on ~350-430K rows take seconds (safe)
- The `COPY` protocol works with partitioned tables — PostgreSQL routes rows to the correct partition automatically
- Transforms query `raw_asset_tasks` (the parent) and see all partitions transparently — zero transform code changes

---

## File Structure

**Create:**
- `swift_api_pipeline/migrations/045_partition_raw_asset_tasks.sql` — migration to convert `raw_asset_tasks` to a partitioned table
- `.github/workflows/pipeline-asset-tasks.yml` — GHA workflow for the full asset tasks pipeline (extract + transform + backfill + analytics + GHA dispatch)

**Modify:**
- `swift_api_pipeline/extract_asset_tasks.py` — add auto-partition creation, per-partition index management, per-project safety check
- `swift_api_pipeline/scheduled_main_pipeline.bat` — remove asset_tasks step (now on GHA), keep backfill/analytics as GHA-triggered or remove entirely

**Not touched:**
- `swift_api_pipeline/transform.py` — queries `raw_asset_tasks` parent table, works transparently with partitions
- `swift_api_pipeline/main.py` — `--pipeline asset_tasks` still works the same way
- `swift_api_pipeline/base_extractor.py`, `swift_api_pipeline/db.py`, `swift_api_pipeline/config.py`
- Any other GHA workflow

---

## Task 1: Migration — Convert `raw_asset_tasks` to a Partitioned Table

**Files:**
- Create: `swift_api_pipeline/migrations/045_partition_raw_asset_tasks.sql`

This migration converts the existing `raw_asset_tasks` table to a partitioned table. PostgreSQL does not support `ALTER TABLE ... PARTITION BY` on an existing table, so we must: rename the old table, create the new partitioned table, create partitions for each known project, migrate data, then drop the old table.

**Important:** This migration must be run during a maintenance window when the pipeline is not running, since it moves ~2.4M rows.

- [ ] **Step 1: Write the migration SQL**

```sql
-- migrations/045_partition_raw_asset_tasks.sql
-- Convert raw_asset_tasks to a partitioned table (by project_did).
-- Each TS project gets its own partition for independent index management
-- and per-project safety checks.
--
-- MUST be run during maintenance window (pipeline not running).
-- Expected runtime: 2-5 minutes for ~2.4M rows.

BEGIN;

-- 1. Rename the old table
ALTER TABLE data_raw.raw_asset_tasks RENAME TO raw_asset_tasks_old;

-- 2. Drop old indexes (they reference the old table)
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_loaded_at;
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_run_id;
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_project_did;

-- 3. Create the new partitioned table (same schema, no BIGSERIAL — partitioned tables
--    can't have SERIAL PKs that span partitions. Use GENERATED ALWAYS instead.)
CREATE TABLE data_raw.raw_asset_tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    project_did TEXT NOT NULL,
    data JSONB NOT NULL
) PARTITION BY LIST (project_did);

-- 4. Create a DEFAULT partition to catch any unknown project_did values.
--    New projects land here until a dedicated partition is created at next run.
CREATE TABLE data_raw.raw_asset_tasks_default
    PARTITION OF data_raw.raw_asset_tasks DEFAULT;

-- 5. Create partitions for each known project.
--    project_did values from reference.ref_ontel_techops_projects where project_number >= 13.
CREATE TABLE data_raw.raw_asset_tasks_ts13
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-NFkG865XjMXlwqZ1AqU');

CREATE TABLE data_raw.raw_asset_tasks_ts14
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-NV5j_QcTmdwoaGklFvf');

CREATE TABLE data_raw.raw_asset_tasks_ts15
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-Np5nDzlfJrK_nt5Ro7e');

CREATE TABLE data_raw.raw_asset_tasks_ts16
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-O99xSQdLiGywc6KRVw-');

CREATE TABLE data_raw.raw_asset_tasks_ts17
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-ONLJdAstPfeGwVNgpYH');

CREATE TABLE data_raw.raw_asset_tasks_ts18
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-O_IpQNpLVwhdVC3QYIm');

CREATE TABLE data_raw.raw_asset_tasks_ts19
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-OmzvGwfYsSskngv6SEo');

-- 6. Create per-partition indexes (fast — each partition is ~350-430K rows)
DO $$
DECLARE
    parts TEXT[] := ARRAY[
        'raw_asset_tasks_ts13', 'raw_asset_tasks_ts14', 'raw_asset_tasks_ts15',
        'raw_asset_tasks_ts16', 'raw_asset_tasks_ts17', 'raw_asset_tasks_ts18',
        'raw_asset_tasks_ts19', 'raw_asset_tasks_default'
    ];
    p TEXT;
BEGIN
    FOREACH p IN ARRAY parts LOOP
        EXECUTE format('CREATE INDEX idx_%s_loaded_at ON data_raw.%I (loaded_at DESC)', p, p);
        EXECUTE format('CREATE INDEX idx_%s_run_id ON data_raw.%I (run_id)', p, p);
    END LOOP;
END $$;

-- 7. Migrate data from old table to new partitioned table.
--    PostgreSQL auto-routes each row to the correct partition by project_did.
INSERT INTO data_raw.raw_asset_tasks (loaded_at, run_id, project_did, data)
SELECT loaded_at, run_id, project_did, data
FROM data_raw.raw_asset_tasks_old;

-- 8. Drop old table
DROP TABLE data_raw.raw_asset_tasks_old;

COMMIT;
```

- [ ] **Step 2: Verify the migration locally**

Connect to Supabase and check:

```sql
-- Confirm partitioned table structure
SELECT
    parent.relname AS parent_table,
    child.relname AS partition,
    pg_get_expr(child.relpartbound, child.oid) AS partition_bound
FROM pg_inherits
JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
JOIN pg_class child ON pg_inherits.inhrelid = child.oid
WHERE parent.relname = 'raw_asset_tasks';

-- Confirm row counts per partition
SELECT tableoid::regclass AS partition, COUNT(*)
FROM data_raw.raw_asset_tasks
GROUP BY tableoid::regclass
ORDER BY 1;

-- Confirm total matches old count
SELECT COUNT(*) FROM data_raw.raw_asset_tasks;
```

Expected: 7 named partitions + 1 default, total ~2.4M rows matching pre-migration count.

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/migrations/045_partition_raw_asset_tasks.sql
git commit -m "migration: partition raw_asset_tasks by project_did for GHA migration"
```

---

## Task 2: Auto-Partition Creation in Extraction Code

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py` (add `ensure_partitions_exist` method)

Before extraction starts, the pipeline must check that every project in the reference table has a corresponding partition. If a new TS project appears (e.g., TS20), the pipeline creates the partition automatically.

- [ ] **Step 1: Add the `ensure_partitions_exist` method to `AssetTaskExtractor`**

In `swift_api_pipeline/extract_asset_tasks.py`, add this method to the `AssetTaskExtractor` class after the `get_project_dids` method (after line 58):

```python
    def ensure_partitions_exist(self, projects: List[Dict]):
        """Auto-create partitions for any project_did that doesn't have one.

        Queries pg_catalog to find existing partitions of raw_asset_tasks,
        then creates missing ones. This allows new TS projects to be picked
        up automatically without manual migration.
        """
        existing = self.db.fetch(
            "SELECT pg_get_expr(c.relpartbound, c.oid) AS bound_expr "
            "FROM pg_inherits i "
            "JOIN pg_class p ON i.inhparent = p.oid "
            "JOIN pg_class c ON i.inhrelid = c.oid "
            "JOIN pg_namespace n ON p.relnamespace = n.oid "
            "WHERE n.nspname = 'data_raw' AND p.relname = 'raw_asset_tasks' "
            "AND c.relname != 'raw_asset_tasks_default'"
        )
        # Extract project_did values from partition bounds like "FOR VALUES IN ('-NFkG...')"
        existing_dids = set()
        for row in existing:
            expr = row["bound_expr"]
            # Parse: FOR VALUES IN ('-NFkG865XjMXlwqZ1AqU')
            start = expr.find("('") + 2
            end = expr.find("')", start)
            if start > 1 and end > start:
                existing_dids.add(expr[start:end])

        for proj in projects:
            did = proj["project_did"]
            name = proj["project_name"].lower().replace(" ", "").replace(":", "_").replace("-", "_")
            if did not in existing_dids:
                partition_name = f"raw_asset_tasks_{name}"
                logger.info(f"Creating partition {partition_name} for {proj['project_name']} ({did})")
                self.db.execute(
                    f"CREATE TABLE IF NOT EXISTS {SCHEMA_RAW}.{partition_name} "
                    f"PARTITION OF {SCHEMA_RAW}.raw_asset_tasks "
                    f"FOR VALUES IN ('{did}')"
                )
                # Create per-partition indexes
                self.db.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{partition_name}_loaded_at "
                    f"ON {SCHEMA_RAW}.{partition_name} (loaded_at DESC)"
                )
                self.db.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{partition_name}_run_id "
                    f"ON {SCHEMA_RAW}.{partition_name} (run_id)"
                )
```

- [ ] **Step 2: Call `ensure_partitions_exist` in the pipeline flow**

In `run_asset_task_pipeline()` (around line 328), add the call after fetching projects but before extraction:

```python
        all_projects = extractor.get_project_dids(min_project_number)

        # ── RECOVERY MODE ─────────────────────────────────────────────────────
        if is_recovery:
            # ... existing recovery code unchanged ...
```

Change to:

```python
        all_projects = extractor.get_project_dids(min_project_number)

        # Ensure all projects have a dedicated partition (auto-creates for new TS projects)
        extractor.ensure_partitions_exist(all_projects)

        # ── RECOVERY MODE ─────────────────────────────────────────────────────
        if is_recovery:
            # ... existing recovery code unchanged ...
```

- [ ] **Step 3: Run the pipeline locally to verify**

```bash
cd swift_api_pipeline
./venv/Scripts/python.exe -u main.py --pipeline asset_tasks --project TS13
```

Expected: No "Creating partition" log lines (all partitions already exist from migration). Pipeline completes normally.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py
git commit -m "feat: auto-create partitions for new TS projects in asset task pipeline"
```

---

## Task 3: Per-Partition Index Management

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py` (update `prepare_table_for_bulk_load` and `restore_table_after_load`)

With partitioned tables, indexes are per-partition. Instead of dropping/rebuilding indexes on a massive 2.4M row table (which caused the pooler timeout), we drop/rebuild on each partition (~350-430K rows each, takes seconds).

- [ ] **Step 1: Replace the global index list with a partition-aware approach**

In `swift_api_pipeline/extract_asset_tasks.py`, replace the `_INDEXES` constant and the `prepare_table_for_bulk_load`/`restore_table_after_load` methods.

Replace the `_INDEXES` block (lines 34-42):

```python
_INDEXES = [
    ("idx_raw_asset_tasks_loaded_at", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_loaded_at ON data_raw.raw_asset_tasks USING btree (loaded_at DESC)"),
    ("idx_raw_asset_tasks_run_id", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_run_id ON data_raw.raw_asset_tasks USING btree (run_id)"),
    ("idx_raw_asset_tasks_project_did", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_project_did ON data_raw.raw_asset_tasks USING btree (project_did)"),
]
# Also drop the GIN index if it still exists (one-time cleanup)
_INDEXES_TO_DROP_ONLY = [
```

With:

```python
# Per-partition index suffixes to drop before bulk load and recreate after.
# With partitioned tables, each partition has its own indexes (~350K rows = seconds to rebuild).
# No more global indexes on the 2.4M-row parent table.
_PARTITION_INDEX_SUFFIXES = ["loaded_at", "run_id"]

# Also drop the GIN index if it still exists (one-time cleanup)
_INDEXES_TO_DROP_ONLY = [
```

- [ ] **Step 2: Update `prepare_table_for_bulk_load` to drop per-partition indexes**

Replace the existing method (lines 224-236):

```python
    def prepare_table_for_bulk_load(self):
        """Drop per-partition indexes for fast bulk loading.

        With partitioned raw_asset_tasks, each partition has its own indexes.
        Dropping them before bulk COPY and recreating after is faster than
        inserting with indexes in place.
        """
        logger.info("Preparing raw_asset_tasks partitions for bulk load (drop indexes)...")
        partitions = self._get_partition_names()
        for part_name in partitions:
            for suffix in _PARTITION_INDEX_SUFFIXES:
                idx_name = f"idx_{part_name}_{suffix}"
                self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        logger.info(f"Indexes dropped on {len(partitions)} partitions")
```

- [ ] **Step 3: Update `restore_table_after_load` to rebuild per-partition indexes**

Replace the existing method (lines 238-248):

```python
    def restore_table_after_load(self):
        """Recreate per-partition indexes after bulk load.

        Each partition is ~350-430K rows — index creation takes seconds,
        well within the Supabase pooler's ~5-6 min connection limit.
        """
        logger.info("Restoring raw_asset_tasks partition indexes...")
        partitions = self._get_partition_names()
        for part_name in partitions:
            logger.info(f"  Indexing {part_name}...")
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{part_name}_loaded_at "
                f"ON {SCHEMA_RAW}.{part_name} (loaded_at DESC)"
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{part_name}_run_id "
                f"ON {SCHEMA_RAW}.{part_name} (run_id)"
            )
        logger.info(f"Indexes restored on {len(partitions)} partitions")
```

- [ ] **Step 4: Add the `_get_partition_names` helper**

Add this method to `AssetTaskExtractor` (after `ensure_partitions_exist`):

```python
    def _get_partition_names(self) -> List[str]:
        """Get all partition table names for raw_asset_tasks (excluding default)."""
        rows = self.db.fetch(
            "SELECT c.relname AS partition_name "
            "FROM pg_inherits i "
            "JOIN pg_class p ON i.inhparent = p.oid "
            "JOIN pg_class c ON i.inhrelid = c.oid "
            "JOIN pg_namespace n ON p.relnamespace = n.oid "
            "WHERE n.nspname = 'data_raw' AND p.relname = 'raw_asset_tasks' "
            "AND c.relname != 'raw_asset_tasks_default' "
            "ORDER BY c.relname"
        )
        return [row["partition_name"] for row in rows]
```

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py
git commit -m "feat: per-partition index management for partitioned raw_asset_tasks"
```

---

## Task 4: Per-Project Safety Check

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py` (update `clear_old_raw_data`)

The current safety check compares total new rows vs total old rows across all projects. This caused April 15's failure — 6 successful projects were thrown away because the combined total fell below 90%. With partitions, we verify each project independently: a project's new data replaces its old data only if it passes the threshold.

- [ ] **Step 1: Replace `clear_old_raw_data` with per-project verification**

Replace the existing `clear_old_raw_data` method (lines 253-291) with:

```python
    def clear_old_raw_data(self, project_rows: dict, failed_projects: list):
        """Clear old raw data per-project, only for projects that passed extraction.

        Each project's partition is verified independently against its own 90%
        threshold. Failed projects keep their old data untouched.
        """
        run_id_str = str(self.run_id)

        for project_name, new_count in project_rows.items():
            if project_name in failed_projects:
                logger.info(f"[{project_name}] Skipped cleanup (extraction failed)")
                continue

            if new_count == 0:
                logger.warning(f"[{project_name}] Skipped cleanup (0 rows extracted)")
                continue

            # Count old rows for this project only
            old_count = self.db.fetchval(
                f'SELECT COUNT(*) FROM {SCHEMA_RAW}.raw_asset_tasks '
                f'WHERE project_did = (SELECT project_did FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects WHERE project_name = $1) '
                f'AND run_id != $2',
                project_name, run_id_str
            ) or 0

            if old_count > 0 and new_count < old_count * self.CLEANUP_ROW_THRESHOLD:
                logger.warning(
                    f"[{project_name}] Cleanup skipped: new={new_count:,}, old={old_count:,} "
                    f"(below {self.CLEANUP_ROW_THRESHOLD:.0%} threshold). Old data retained."
                )
                continue

            logger.info(
                f"[{project_name}] Verified: new={new_count:,}, old={old_count:,}. Cleaning up."
            )
            retry_db(
                lambda pn=project_name, rid=run_id_str: self.db.execute(
                    f'DELETE FROM {SCHEMA_RAW}.raw_asset_tasks '
                    f'WHERE project_did = (SELECT project_did FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects WHERE project_name = $1) '
                    f'AND run_id != $2',
                    pn, rid
                ),
                description=f"delete old raw data for {project_name}"
            )

        logger.info("Per-project cleanup complete")
```

- [ ] **Step 2: Update the caller in `run_asset_task_pipeline` to pass project_rows and failed_projects**

In the `run_asset_task_pipeline` function, find the line (around line 521):

```python
        # Clean up old raw data now that new extraction succeeded
        extractor.clear_old_raw_data()
```

Replace with:

```python
        # Clean up old raw data — per-project verification
        extractor.clear_old_raw_data(project_rows, failed_projects)
```

- [ ] **Step 3: Update failure handling — partial success is no longer a hard failure**

Find the block (around lines 533-549) that raises RuntimeError on partial failure:

```python
        # Detect partial failures — projects that failed even after retry
        if failed_projects:
            extractor.complete_pipeline_run("failed", total_records,
                                            error=f"Projects failed: {', '.join(failed_projects)}")
            logger.error(f"\n{'='*60}")
            logger.error(f"Pipeline PARTIAL FAILURE")
            ...
            raise RuntimeError(
                f"Asset tasks partial failure: {', '.join(failed_projects)} "
                f"failed ({total_records:,} of expected rows loaded)"
            )
```

Replace with:

```python
        # Detect partial failures — successful projects still have fresh data
        if failed_projects:
            extractor.complete_pipeline_run("partial", total_records,
                                            error=f"Projects failed: {', '.join(failed_projects)}")
            logger.warning(f"\n{'='*60}")
            logger.warning(f"Pipeline PARTIAL SUCCESS ({len(project_rows) - len(failed_projects)}/{len(project_rows)} projects)")
            logger.warning(f"\nRecords by project:")
            for name, count in sorted(project_rows.items()):
                status = " [FAILED - old data retained]" if name in failed_projects else ""
                logger.warning(f"  {name}: {count:,}{status}")
            logger.warning(f"\nTotal loaded: {total_records:,}")
            logger.warning(f"Failed projects: {', '.join(failed_projects)}")
            logger.warning(f"Run ID: {extractor.run_id}")
            logger.warning(f"{'='*60}\n")
            # Don't raise — let transforms run on available data.
            # The failed projects retain their old data in their partitions.
```

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py
git commit -m "feat: per-project safety check and partial success for partitioned pipeline"
```

---

## Task 5: GHA Workflow

**Files:**
- Create: `.github/workflows/pipeline-asset-tasks.yml`

This workflow runs the full asset tasks pipeline (extract + transform + backfill + analytics) on a nightly cron schedule. It follows the same pattern as the existing `pipeline-timer.yml` workflow.

- [ ] **Step 1: Create the workflow file**

```yaml
name: "Pipeline: Asset Tasks"

on:
  schedule:
    - cron: '1 4 * * *'  # 04:01 UTC = 00:01 EDT (matches old local schedule)
  repository_dispatch:
    types: [pipeline-asset-tasks]
  workflow_dispatch:
    inputs:
      project:
        description: 'Single project recovery (e.g. TS16). Leave empty for full run.'
        required: false
        type: string

concurrency:
  group: pipeline-asset-tasks
  cancel-in-progress: false

env:
  PIPELINE_DIR: swift_api_pipeline

jobs:
  asset-tasks:
    runs-on: ubuntu-latest
    timeout-minutes: 90

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

      - name: Run asset tasks pipeline
        working-directory: ${{ env.PIPELINE_DIR }}
        run: |
          if [ -n "${{ inputs.project }}" ]; then
            echo "Recovery mode: project=${{ inputs.project }}"
            python -u main.py --pipeline asset_tasks --project "${{ inputs.project }}"
          else
            python -u main.py --pipeline asset_tasks
          fi

      - name: Run backfill
        if: ${{ !inputs.project }}
        working-directory: ${{ env.PIPELINE_DIR }}
        run: python -u main.py --pipeline backfill --no-email

      - name: Run analytics refresh
        if: ${{ !inputs.project }}
        working-directory: ${{ env.PIPELINE_DIR }}
        run: python -u main.py --pipeline analytics --no-email

      - name: Dispatch downstream workflows
        if: ${{ !inputs.project }}
        run: |
          for event in pipeline-asset-tasks-export pipeline-timer-discrepancies; do
            curl -s -X POST \
              -H "Authorization: Bearer ${{ secrets.GITHUB_TOKEN }}" \
              -H "Accept: application/vnd.github+json" \
              "https://api.github.com/repos/${{ github.repository }}/dispatches" \
              -d "{\"event_type\": \"$event\"}"
            echo "Dispatched: $event"
          done
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/pipeline-asset-tasks.yml
git commit -m "feat: add GHA workflow for nightly asset tasks pipeline"
```

---

## Task 6: Update Local Batch File

**Files:**
- Modify: `swift_api_pipeline/scheduled_main_pipeline.bat`

The local batch file should no longer run asset_tasks, backfill, or analytics — those now run on GHA. The batch file becomes a thin dispatcher only (or is removed entirely once GHA is confirmed stable).

- [ ] **Step 1: Update `scheduled_main_pipeline.bat`**

Replace the entire content with a simplified version that only dispatches GHA if needed as a fallback:

```batch
@echo off
setlocal enabledelayedexpansion
REM Swift API Pipeline - Nightly Local Run
REM As of 2026-04-XX, the asset tasks pipeline runs on GitHub Actions.
REM This file is kept as a manual fallback only.
REM To trigger the GHA pipeline manually:
REM   gh workflow run "Pipeline: Asset Tasks"
REM
REM The old nightly flow (asset_tasks -> backfill -> analytics -> dispatch)
REM now runs entirely on GHA via cron at 04:01 UTC (00:01 EDT).

echo [%date% %time%] Asset tasks pipeline now runs on GitHub Actions.
echo [%date% %time%] Use 'gh workflow run "Pipeline: Asset Tasks"' to trigger manually.
echo [%date% %time%] Or visit: https://github.com/jamilmendez-ontel/local-pipeline/actions
```

- [ ] **Step 2: Disable or update the Windows Task Scheduler task**

This is a manual step. In Windows Task Scheduler:
- Find the `SwiftPipeline-Main` task (or whatever it's named)
- Either disable it or update it to not run `scheduled_main_pipeline.bat`
- The GHA cron schedule handles the nightly run now

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/scheduled_main_pipeline.bat
git commit -m "chore: retire local asset tasks pipeline in favor of GHA"
```

---

## Task 7: End-to-End Verification

**Files:** None (verification only)

- [ ] **Step 1: Verify partitions are set up correctly**

```sql
SELECT tableoid::regclass AS partition, COUNT(*)
FROM data_raw.raw_asset_tasks
GROUP BY tableoid::regclass
ORDER BY 1;
```

Expected: 7 partitions with ~350-430K rows each, total ~2.4M.

- [ ] **Step 2: Trigger the GHA workflow manually**

```bash
gh workflow run "Pipeline: Asset Tasks"
```

Monitor the run in the GitHub Actions UI. Expected:
- Extraction completes in ~45-60 min
- No pooler timeout errors on index rebuilds
- Transforms succeed (they query the parent table transparently)
- Backfill and analytics complete
- Downstream workflows dispatched

- [ ] **Step 3: Verify per-project safety check by simulating a failure**

Trigger a single-project recovery:

```bash
gh workflow run "Pipeline: Asset Tasks" -f project=TS13
```

Expected: Only TS13 re-extracted, other partitions untouched.

- [ ] **Step 4: Verify auto-partition creation**

Add a fake project to the reference table (or wait for a real TS20), run the pipeline, and confirm:
- "Creating partition raw_asset_tasks_techops_ts20" appears in logs
- The new partition is created and indexed automatically
- Data is written to it

- [ ] **Step 5: Keep the local pipeline available for 1 week as fallback**

Don't disable the Windows Task Scheduler task immediately. Run both in parallel for ~1 week to confirm GHA is stable, then disable the local task.
