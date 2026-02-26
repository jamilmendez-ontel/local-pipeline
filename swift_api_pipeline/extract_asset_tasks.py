#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for specified projects.

Architecture: 6 extraction workers each write directly to DB after every API page.
No Queue or separate loader threads — extraction and loading happen simultaneously.
Before bulk load: table set to UNLOGGED and non-PK indexes dropped.
After load: indexes recreated and table set back to LOGGED.
"""

import uuid
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict
from config import (
    SCHEMA_RAW, SCHEMA_REFERENCE, SCHEMA_PIPELINE, get_logger, retry_db
)
from base_extractor import BaseExtractor

logger = get_logger("asset_tasks")

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 6  # Concurrent API + DB writer threads
LOAD_BATCH_SIZE = 25000
RETRY_WAIT_SECONDS = 300  # Wait before project-level retry (5 min)

# Non-PK indexes to drop before bulk load and recreate after.
# GIN index on data column permanently dropped — costs ~2.4GB, never used by pipeline or agent
# (pipeline uses run_id/project_did for lookups; agent queries staging, not raw).
_INDEXES = [
    ("idx_raw_asset_tasks_loaded_at", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_loaded_at ON data_raw.raw_asset_tasks USING btree (loaded_at DESC)"),
    ("idx_raw_asset_tasks_run_id", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_run_id ON data_raw.raw_asset_tasks USING btree (run_id)"),
    ("idx_raw_asset_tasks_project_did", "CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_project_did ON data_raw.raw_asset_tasks USING btree (project_did)"),
]
# Also drop the GIN index if it still exists (one-time cleanup)
_INDEXES_TO_DROP_ONLY = [
    "idx_raw_asset_tasks_data",
]


class AssetTaskExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="asset_tasks_extract")

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get project DIDs from reference table"""
        rows = self.db.fetch(
            f'SELECT project_did, project_name, project_number '
            f'FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects '
            f'WHERE project_number >= $1 '
            f'ORDER BY project_number',
            min_project_number
        )
        return [dict(r) for r in rows]

    def extract_and_load_project(
        self,
        project_did: str,
        project_name: str,
    ) -> int:
        """Extract all asset-tasks for a single project and write directly to DB.
        Each API page (1000 rows) is accumulated locally, then flushed in LOAD_BATCH_SIZE chunks."""
        if not self.token:
            self.authenticate()

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/next/projects/{project_did}/assets/_export"

        params = {
            "pageSize": PAGE_SIZE,
            "dateFormat": "yyyy-MM-dd",
            "timezone": "America/New_York"
        }

        after_ap = None
        after_id = None
        page_count = 0
        project_rows = 0
        run_id_str = str(self.run_id)
        pending = []  # accumulate before flushing

        logger.info(f"[{project_name}] Starting extraction...")

        while True:
            if after_ap and after_id:
                params['afterAp'] = after_ap
                params['after'] = after_id

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)

                    if resp.status_code == 204:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        # Flush remaining
                        if pending:
                            self._write_batch(run_id_str, project_did, pending)
                            pending = []
                        return project_rows

                    resp.raise_for_status()
                    data = resp.json().get("list", [])

                    if not data:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        if pending:
                            self._write_batch(run_id_str, project_did, pending)
                            pending = []
                        return project_rows

                    pending.extend(data)
                    project_rows += len(data)
                    page_count += 1

                    # Write to DB when batch is large enough
                    while len(pending) >= LOAD_BATCH_SIZE:
                        batch = pending[:LOAD_BATCH_SIZE]
                        pending = pending[LOAD_BATCH_SIZE:]
                        self._write_batch(run_id_str, project_did, batch)

                    if page_count % 50 == 0:
                        logger.info(f"[{project_name}] Page {page_count} - {project_rows:,} rows")

                    # Handle keyset pagination
                    next_info = resp.json().get("next")
                    if not next_info:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        if pending:
                            self._write_batch(run_id_str, project_did, pending)
                            pending = []
                        return project_rows

                    after_ap = next_info.get("ap")
                    after_id = next_info.get("id")
                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    logger.error(f"[{project_name}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        self.reauthenticate()
                        headers = self.get_auth_headers()
            else:
                raise RuntimeError(f"[{project_name}] Failed after {MAX_RETRIES} attempts")

    def _write_batch(self, run_id_str: str, project_did: str, records: list):
        """Write a batch of records directly to raw_asset_tasks via COPY."""
        tuples = [(run_id_str, project_did, rec) for rec in records]
        retry_db(
            lambda: self.db.copy_records(
                "raw_asset_tasks",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "project_did", "data"],
            ),
            description="copy raw_asset_tasks"
        )
        self.increment_loaded(len(records))

    def prepare_table_for_bulk_load(self):
        """Drop non-PK indexes for fast bulk loading.

        Note: UNLOGGED removed — Supabase's connection proxy kills long-running
        ALTER TABLE SET LOGGED operations (>5 min for 2.2M rows). Index drop/recreate
        provides the main speed benefit anyway.
        """
        logger.info("Preparing raw_asset_tasks for bulk load (drop indexes)...")
        for idx_name, _ in _INDEXES:
            self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        for idx_name in _INDEXES_TO_DROP_ONLY:
            self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        logger.info("Indexes dropped")

    def restore_table_after_load(self):
        """Recreate indexes after bulk load.

        Uses 600s timeout for index creation — project_did index on 2.2M rows
        can take >300s (observed 338s on 2026-02-15).
        """
        logger.info("Restoring raw_asset_tasks indexes...")
        for idx_name, idx_def in _INDEXES:
            logger.info(f"  Creating {idx_name}...")
            self.db.execute(idx_def, statement_timeout=600)
        logger.info("Indexes restored")

    def clear_old_raw_data(self):
        """Clear old raw data (keep current run_id). Single query."""
        logger.info(f"Cleaning up old raw data (keeping run_id={self.run_id})...")
        retry_db(
            lambda: self.db.execute(
                f'DELETE FROM {SCHEMA_RAW}.raw_asset_tasks WHERE run_id != $1',
                str(self.run_id)
            ),
            description="delete old raw_asset_tasks"
        )
        logger.info("Old raw data cleaned up")

    # start_pipeline_run() and complete_pipeline_run() inherited from BaseExtractor


def run_asset_task_pipeline(
    min_project_number: int = 13,
    max_workers: int = MAX_WORKERS,
    project_filter: str = None,
):
    """Main pipeline for extracting asset-tasks with parallel processing.

    Each worker extracts from API and writes directly to DB — no Queue overhead.
    Table is set to UNLOGGED with indexes dropped during bulk load for maximum throughput.

    project_filter: if set, runs in single-project recovery mode.
        Reuses the latest pipeline run_id, cleans only that project's raw rows,
        re-extracts, and marks the run as success. No index drop/restore.
        Use with: python main.py --pipeline asset_tasks --project TS16
    """
    is_recovery = project_filter is not None

    logger.info(f"\n{'='*60}")
    if is_recovery:
        logger.info(f"Asset-Task Extraction Pipeline (Recovery: project_filter='{project_filter}')")
    else:
        logger.info(f"Asset-Task Extraction Pipeline (Direct Write)")
        logger.info(f"Projects: TECH-OPS TS{min_project_number}+")
        logger.info(f"Workers: {max_workers}")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    extractor = AssetTaskExtractor()

    try:
        extractor.authenticate()

        all_projects = extractor.get_project_dids(min_project_number)

        # ── RECOVERY MODE ─────────────────────────────────────────────────────
        if is_recovery:
            # Reuse the latest run_id (success or failed) — don't create a new run
            run_row = extractor.db.fetchrow(
                f"SELECT run_id, records_extracted FROM {SCHEMA_PIPELINE}.pipeline_runs "
                f"WHERE pipeline_name = 'asset_tasks_extract' ORDER BY started_at DESC LIMIT 1"
            )
            if run_row is None:
                raise ValueError("No previous asset_tasks_extract run found to recover")

            extractor.run_id = uuid.UUID(str(run_row["run_id"]))
            existing_rows = run_row["records_extracted"] or 0

            # Filter to the single matching project
            projects = [p for p in all_projects if project_filter in p["project_name"]]
            if not projects:
                raise ValueError(
                    f"No project found matching '{project_filter}'. "
                    f"Available: {[p['project_name'] for p in all_projects]}"
                )
            proj = projects[0]

            logger.info(f"Recovery mode: matched project '{proj['project_name']}'")
            logger.info(f"Reusing run_id={extractor.run_id}, existing_rows={existing_rows:,}")

            # Count stale rows for this project before cleaning
            old_row = extractor.db.fetchrow(
                f"SELECT COUNT(*) AS cnt FROM {SCHEMA_RAW}.raw_asset_tasks "
                f"WHERE project_did=$1 AND run_id=$2",
                proj["project_did"], str(extractor.run_id)
            )
            old_project_rows = old_row["cnt"] if old_row is not None else 0
            logger.info(f"[{proj['project_name']}] Removing {old_project_rows:,} stale rows before re-extraction")

            retry_db(
                lambda did=proj["project_did"], rid=str(extractor.run_id): extractor.db.execute(
                    f"DELETE FROM {SCHEMA_RAW}.raw_asset_tasks WHERE project_did=$1 AND run_id=$2",
                    did, rid
                ),
                description=f"clean partial raw data for {proj['project_name']}"
            )

            new_rows = extractor.extract_and_load_project(proj["project_did"], proj["project_name"])
            new_total = existing_rows - old_project_rows + new_rows

            extractor.complete_pipeline_run("success", new_total)

            logger.info(f"\n{'='*60}")
            logger.info(f"Recovery completed successfully")
            logger.info(f"  Project:       {proj['project_name']}")
            logger.info(f"  Rows extracted: {new_rows:,}")
            logger.info(f"  Updated total:  {new_total:,}")
            logger.info(f"  Run ID:         {extractor.run_id}")
            logger.info(f"{'='*60}\n")

            return str(extractor.run_id)

        # ── NORMAL (FULL) MODE ────────────────────────────────────────────────
        extractor.start_pipeline_run()

        projects = all_projects
        logger.info(f"Found {len(projects)} projects to process\n")

        # Prepare table for fast bulk loading
        extractor.prepare_table_for_bulk_load()

        # Extract and load projects in parallel — each worker writes directly to DB
        project_rows = {}
        failed_projects = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    extractor.extract_and_load_project,
                    proj["project_did"],
                    proj["project_name"],
                ): proj
                for proj in projects
            }

            for future in as_completed(futures):
                proj = futures[future]
                try:
                    rows = future.result()
                    project_rows[proj["project_name"]] = rows
                except Exception as e:
                    logger.error(f"[{proj['project_name']}] FAILED: {type(e).__name__}: {e}")
                    project_rows[proj["project_name"]] = 0
                    failed_projects.append(proj["project_name"])

        # ── Project-level auto-retry (before index restore — faster writes) ──
        if failed_projects:
            logger.warning(
                f"Retrying {len(failed_projects)} failed project(s) after "
                f"{RETRY_WAIT_SECONDS}s: {failed_projects}"
            )
            time.sleep(RETRY_WAIT_SECONDS)

            still_failed = []
            for proj_name in failed_projects:
                proj = next(p for p in projects if p["project_name"] == proj_name)
                # Clean partial data from first attempt before retrying
                retry_db(
                    lambda did=proj["project_did"], rid=str(extractor.run_id): extractor.db.execute(
                        f"DELETE FROM {SCHEMA_RAW}.raw_asset_tasks WHERE project_did=$1 AND run_id=$2",
                        did, rid
                    ),
                    description=f"clean partial raw data for {proj_name}"
                )
                try:
                    rows = extractor.extract_and_load_project(proj["project_did"], proj["project_name"])
                    project_rows[proj_name] = rows
                    logger.info(f"[{proj_name}] Retry SUCCEEDED: {rows:,} rows")
                except Exception as e:
                    logger.error(f"[{proj_name}] Retry FAILED: {type(e).__name__}: {e}")
                    still_failed.append(proj_name)

            failed_projects = still_failed  # Only projects that failed even after retry

        total_records = extractor.total_loaded

        # Restore table: recreate indexes
        extractor.restore_table_after_load()

        # Clean up old raw data now that new extraction succeeded
        extractor.clear_old_raw_data()

        # Detect partial failures — projects that failed even after retry
        if failed_projects:
            extractor.complete_pipeline_run("failed", total_records,
                                            error=f"Projects failed: {', '.join(failed_projects)}")
            logger.error(f"\n{'='*60}")
            logger.error(f"Pipeline PARTIAL FAILURE")
            logger.error(f"\nRecords by project:")
            for name, count in sorted(project_rows.items()):
                status = " [FAILED]" if name in failed_projects else ""
                logger.error(f"  {name}: {count:,}{status}")
            logger.error(f"\nTotal loaded: {total_records:,}")
            logger.error(f"Failed projects: {', '.join(failed_projects)}")
            logger.error(f"Run ID: {extractor.run_id}")
            logger.error(f"{'='*60}\n")
            raise RuntimeError(
                f"Asset tasks partial failure: {', '.join(failed_projects)} "
                f"failed ({total_records:,} of expected rows loaded)"
            )

        extractor.complete_pipeline_run("success", total_records)

        logger.info(f"\n{'='*60}")
        logger.info(f"Pipeline completed successfully")
        logger.info(f"\nRecords by project:")
        for name, count in sorted(project_rows.items()):
            logger.info(f"  {name}: {count:,}")
        logger.info(f"\nTotal loaded: {total_records:,}")
        logger.info(f"Run ID: {extractor.run_id}")
        logger.info(f"{'='*60}\n")

        return str(extractor.run_id)

    except Exception as e:
        logger.error(f"\n{'='*60}")
        logger.error(f"Pipeline failed: {e}")
        logger.error(f"{'='*60}\n")
        if not is_recovery:
            # Try to restore table state even on failure (only needed in full mode)
            try:
                extractor.restore_table_after_load()
            except Exception as restore_err:
                logger.error(f"Failed to restore table: {restore_err}")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract asset-tasks from Swift API")
    parser.add_argument("--min-project", type=int, default=13, help="Minimum project number (default: 13)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    parser.add_argument("--project", type=str, metavar="TS16", help="Recover a single project (e.g. TS16)")
    args = parser.parse_args()

    run_asset_task_pipeline(
        min_project_number=args.min_project,
        max_workers=args.workers,
        project_filter=args.project,
    )
