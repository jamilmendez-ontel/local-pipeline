#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for specified projects.

Architecture: 6 extraction workers each write directly to DB after every API page.
No Queue or separate loader threads — extraction and loading happen simultaneously.
Before bulk load: table set to UNLOGGED and non-PK indexes dropped.
After load: indexes recreated and table set back to LOGGED.
"""

import re
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
from pipeline_notifier import PipelineOutcome

logger = get_logger("asset_tasks")

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 6  # Concurrent API + DB writer threads
LOAD_BATCH_SIZE = 25000
RETRY_WAIT_SECONDS = 300  # Wait before project-level retry (5 min)
MAX_RETRY_ROUNDS = 3        # project-level retry rounds after the initial extraction
ABNORMAL_DROP_PCT = 0.10    # flag a project if its rows drop > 10% vs the previous successful run
PROJECT_TIMEOUT_SECONDS = 3600  # Max 1 hour per project extraction


def detect_abnormal_counts(current_counts, baseline_counts, drop_pct=ABNORMAL_DROP_PCT):
    """Return sorted project names whose row count looks abnormal.

    A project is abnormal if it returned 0 rows, or (with a positive baseline)
    fell more than drop_pct below the previous successful run. Projects with no
    baseline and a nonzero count are skipped so first-ever extractions don't
    false-alarm.
    """
    abnormal = []
    for name, count in current_counts.items():
        if count == 0:
            abnormal.append(name)
            continue
        base = baseline_counts.get(name)
        if base and base > 0 and count < base * (1 - drop_pct):
            abnormal.append(name)
    return sorted(abnormal)


def _retry_loop(retry_once, failed_projects, max_rounds=MAX_RETRY_ROUNDS,
                wait_seconds=RETRY_WAIT_SECONDS, sleep=time.sleep):
    """Retry failing projects up to max_rounds times, resting between rounds.

    retry_once(failed) performs one retry pass and returns the names still
    failing. Stops early once nothing remains.
    """
    still = list(failed_projects)
    for round_no in range(1, max_rounds + 1):
        if not still:
            break
        logger.warning(
            f"Retry round {round_no}/{max_rounds} for {len(still)} project(s) "
            f"after {wait_seconds}s rest: {still}"
        )
        sleep(wait_seconds)
        still = retry_once(still)
    return still


# Non-PK indexes to drop before bulk load and recreate after.
# GIN index on data column permanently dropped — costs ~2.4GB, never used by pipeline or agent
# (pipeline uses run_id/project_did for lookups; agent queries staging, not raw).
# Per-partition index suffixes (applied to every partition of raw_asset_tasks).
# Migration 052 partitioned raw_asset_tasks BY LIST (project_did), so each
# partition is ~350K rows and indexes drop/recreate in seconds — well within
# Supabase pooler's connection lifetime. No more global indexes on the parent.
_PARTITION_INDEX_SUFFIXES = ["loaded_at", "run_id"]

# One-time cleanup: drop any GIN index left over from earlier table shapes.
# Plus the old GLOBAL btree indexes that pre-052 code may recreate against
# the now-partitioned parent (those still propagate to children and end up
# as duplicates of the per-partition indexes; clean them up on every run so
# the system self-heals if an older deployment touches the table).
_INDEXES_TO_DROP_ONLY = [
    "idx_raw_asset_tasks_data",
    "idx_raw_asset_tasks_loaded_at",
    "idx_raw_asset_tasks_run_id",
    "idx_raw_asset_tasks_project_did",
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

    @staticmethod
    def _partition_suffix(project_name: str) -> str:
        """Derive a partition-name suffix from a project_name.

        Convention matches migration 052: 'TECH-OPS: TS19' -> 'ts19'.
        Falls back to a sanitized full name if no colon is present.
        """
        tail = project_name.split(":")[-1].strip().lower()
        return re.sub(r'[^a-z0-9_]+', '_', tail).strip('_')

    def ensure_partitions_exist(self, projects: List[Dict]):
        """Auto-create partitions for any project_did that doesn't have one.

        Queries pg_catalog for existing partition bounds of raw_asset_tasks,
        then creates a partition (and its per-partition indexes) for any
        project whose project_did isn't already covered. Lets new TS projects
        be picked up automatically without a manual migration.

        Safe to call on every run — no-op when all partitions already exist.
        """
        existing = self.db.fetch(
            "SELECT pg_get_expr(c.relpartbound, c.oid) AS bound_expr "
            "FROM pg_inherits i "
            "JOIN pg_class p ON i.inhparent = p.oid "
            "JOIN pg_class c ON i.inhrelid = c.oid "
            "JOIN pg_namespace n ON p.relnamespace = n.oid "
            "WHERE n.nspname = $1 AND p.relname = $2 "
            "AND c.relname != 'raw_asset_tasks_default'",
            SCHEMA_RAW, "raw_asset_tasks"
        )
        # Extract project_did from each bound expression: "FOR VALUES IN ('-...')".
        existing_dids = set()
        for row in existing:
            expr = row["bound_expr"] or ""
            start = expr.find("('") + 2
            end = expr.find("')", start)
            if start > 1 and end > start:
                existing_dids.add(expr[start:end])

        for proj in projects:
            did = proj["project_did"]
            if did in existing_dids:
                continue
            suffix = self._partition_suffix(proj["project_name"])
            if not suffix:
                logger.warning(
                    f"Skipping auto-partition for {proj['project_name']!r} "
                    f"({did}): unable to derive a valid partition suffix"
                )
                continue
            partition_name = f"raw_asset_tasks_{suffix}"
            logger.info(
                f"Creating partition {partition_name} for {proj['project_name']} ({did})"
            )
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_RAW}.{partition_name} "
                f"PARTITION OF {SCHEMA_RAW}.raw_asset_tasks "
                f"FOR VALUES IN ('{did}')"
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{partition_name}_loaded_at "
                f"ON {SCHEMA_RAW}.{partition_name} (loaded_at DESC)"
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{partition_name}_run_id "
                f"ON {SCHEMA_RAW}.{partition_name} (run_id)"
            )

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
        start_time = time.monotonic()

        # Check for resume point from a prior interrupted extraction
        progress = self.db.fetchrow(
            f'SELECT rows_written, after_ap, after_id '
            f'FROM {SCHEMA_PIPELINE}.extraction_progress '
            f'WHERE run_id = $1 AND project_did = $2',
            run_id_str, project_did
        )
        if progress and progress["rows_written"] > 0 and progress["after_ap"]:
            project_rows = progress["rows_written"]
            after_ap = progress["after_ap"]
            after_id = progress["after_id"]
            page_count = project_rows // PAGE_SIZE
            logger.info(
                f"[{project_name}] Resuming from page {page_count} "
                f"({project_rows:,} rows already written)"
            )
        else:
            logger.info(f"[{project_name}] Starting extraction...")

        while True:
            # Guard against hung workers — bail if project exceeds timeout
            elapsed = time.monotonic() - start_time
            if elapsed > PROJECT_TIMEOUT_SECONDS:
                if pending:
                    self._write_batch(run_id_str, project_did, pending,
                                      after_ap=after_ap, after_id=after_id)
                raise TimeoutError(
                    f"[{project_name}] Exceeded {PROJECT_TIMEOUT_SECONDS}s timeout "
                    f"after {project_rows:,} rows ({page_count} pages)"
                )
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
                        self._write_batch(run_id_str, project_did, batch,
                                          after_ap=after_ap, after_id=after_id)

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

    def _write_batch(self, run_id_str: str, project_did: str, records: list,
                     after_ap: str = None, after_id: str = None):
        """Write a batch of records directly to raw_asset_tasks via COPY.
        Verifies the COPY result and saves cursor for resume capability."""
        expected = len(records)
        tuples = [(run_id_str, project_did, rec) for rec in records]
        result = retry_db(
            lambda: self.db.copy_records(
                "raw_asset_tasks",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "project_did", "data"],
            ),
            description="copy raw_asset_tasks"
        )
        # Verify rows persisted — asyncpg returns "COPY <n>"
        if result:
            try:
                actual = int(result.split()[-1])
            except (ValueError, IndexError):
                actual = -1
            if actual != expected:
                raise RuntimeError(
                    f"COPY verification failed: expected {expected} rows, got '{result}'"
                )
        self.increment_loaded(expected)

        # Save cursor for resume-on-failure
        if after_ap is not None or after_id is not None:
            retry_db(
                lambda: self.db.execute(
                    f'INSERT INTO {SCHEMA_PIPELINE}.extraction_progress '
                    f'(run_id, project_did, rows_written, after_ap, after_id, updated_at) '
                    f'VALUES ($1, $2, $3, $4, $5, NOW()) '
                    f'ON CONFLICT (run_id, project_did) DO UPDATE SET '
                    f'rows_written = pipeline.extraction_progress.rows_written + $3, '
                    f'after_ap = $4, after_id = $5, updated_at = NOW()',
                    run_id_str, project_did, expected, after_ap, after_id
                ),
                description="upsert extraction_progress"
            )

    def prepare_table_for_bulk_load(self):
        """Drop non-PK indexes for fast bulk loading.

        Note: UNLOGGED removed — Supabase's connection proxy kills long-running
        ALTER TABLE SET LOGGED operations (>5 min for 2.2M rows). Per-partition
        index drop/recreate provides the main speed benefit anyway.

        Since migration 052 partitioned raw_asset_tasks, indexes are per
        partition (~350K rows each — drops/recreates in seconds).
        """
        logger.info("Preparing raw_asset_tasks partitions for bulk load (drop indexes)...")
        partitions = self._get_partition_names()
        for part_name in partitions:
            for suffix in _PARTITION_INDEX_SUFFIXES:
                idx_name = f"idx_{part_name}_{suffix}"
                self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        # One-time cleanup of stale/legacy global indexes (no-op once gone).
        for idx_name in _INDEXES_TO_DROP_ONLY:
            self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        logger.info(f"Indexes dropped on {len(partitions)} partitions")

    def restore_table_after_load(self):
        """Recreate per-partition indexes after bulk load.

        Each partition is ~350K rows — index creation takes seconds, well
        within the Supabase pooler's ~5-6 min connection limit. No global
        indexes on the parent (those would propagate to children and
        duplicate the per-partition ones).
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

    def _get_partition_names(self) -> List[str]:
        """All raw_asset_tasks partition table names (excludes the default)."""
        rows = self.db.fetch(
            "SELECT c.relname AS partition_name "
            "FROM pg_inherits i "
            "JOIN pg_class p ON i.inhparent = p.oid "
            "JOIN pg_class c ON i.inhrelid = c.oid "
            "JOIN pg_namespace n ON p.relnamespace = n.oid "
            "WHERE n.nspname = $1 AND p.relname = $2 "
            "AND c.relname != 'raw_asset_tasks_default' "
            "ORDER BY c.relname",
            SCHEMA_RAW, "raw_asset_tasks"
        )
        return [row["partition_name"] for row in rows]

    # New run must have at least 90% of old run rows to allow cleanup
    CLEANUP_ROW_THRESHOLD = 0.90

    def clear_old_raw_data(self, projects: List[Dict], project_rows: dict, failed_projects: list):
        """Clear old raw data per project, only for projects that passed extraction.

        Each partition (one per project_did) is verified independently against
        its own 90% threshold. A single project's failure no longer invalidates
        the other six's fresh data — fixes the baseline-inflation behavior that
        used to throw away successful projects on a partial failure.

        Args:
            projects:        full list of projects from get_project_dids() —
                             provides the project_name -> project_did mapping
                             without an extra subquery per check.
            project_rows:    {project_name: new_row_count} from this run.
            failed_projects: project names that failed even after retry.
                             Their partitions are left untouched.
        """
        run_id_str = str(self.run_id)
        did_by_name = {p["project_name"]: p["project_did"] for p in projects}

        kept_count = 0
        cleaned_count = 0
        skipped = []

        for project_name, new_count in project_rows.items():
            if project_name in failed_projects:
                logger.info(f"[{project_name}] Skipped cleanup (extraction failed)")
                skipped.append((project_name, "extraction failed", new_count))
                kept_count += 1
                continue

            if new_count == 0:
                logger.warning(f"[{project_name}] Skipped cleanup (0 rows extracted)")
                skipped.append((project_name, "0 new rows", new_count))
                kept_count += 1
                continue

            did = did_by_name.get(project_name)
            if did is None:
                logger.warning(
                    f"[{project_name}] Skipped cleanup (no project_did in lookup) — "
                    f"this should not happen"
                )
                skipped.append((project_name, "no project_did", new_count))
                kept_count += 1
                continue

            old_count = self.db.fetchval(
                f'SELECT COUNT(*) FROM {SCHEMA_RAW}.raw_asset_tasks '
                f'WHERE project_did = $1 AND run_id != $2',
                did, run_id_str
            ) or 0

            if old_count > 0 and new_count < old_count * self.CLEANUP_ROW_THRESHOLD:
                logger.warning(
                    f"[{project_name}] Cleanup skipped: new={new_count:,}, "
                    f"old={old_count:,} (below {self.CLEANUP_ROW_THRESHOLD:.0%} threshold). "
                    f"Old data retained."
                )
                skipped.append((project_name, f"below threshold ({new_count}/{old_count})", new_count))
                kept_count += 1
                continue

            logger.info(
                f"[{project_name}] Verified: new={new_count:,}, old={old_count:,}. "
                f"Cleaning up old partition data."
            )
            retry_db(
                lambda d=did, rid=run_id_str: self.db.execute(
                    f'DELETE FROM {SCHEMA_RAW}.raw_asset_tasks '
                    f'WHERE project_did = $1 AND run_id != $2',
                    d, rid
                ),
                description=f"delete old raw data for {project_name}"
            )
            cleaned_count += 1

        logger.info(
            f"Per-project cleanup complete: {cleaned_count} cleaned, {kept_count} retained"
        )
        return skipped  # Caller can decide whether to surface in run summary

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

        # Ensure every project has a dedicated partition. No-op when all
        # partitions already exist — new TS projects are auto-partitioned here.
        extractor.ensure_partitions_exist(all_projects)

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

            return PipelineOutcome(run_id=str(extractor.run_id))

        # ── NORMAL (FULL) MODE ────────────────────────────────────────────────
        extractor.start_pipeline_run()

        projects = all_projects
        logger.info(f"Found {len(projects)} projects to process\n")

        # Prepare table for fast bulk loading
        extractor.prepare_table_for_bulk_load()

        # Extract and load projects in parallel — each worker writes directly to DB
        # Overall timeout: PROJECT_TIMEOUT_SECONDS + 5 min buffer for all workers
        overall_timeout = PROJECT_TIMEOUT_SECONDS + 300
        project_rows = {}
        failed_projects = []
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(
                    extractor.extract_and_load_project,
                    proj["project_did"],
                    proj["project_name"],
                ): proj
                for proj in projects
            }

            timed_out = False
            try:
                for future in as_completed(futures, timeout=overall_timeout):
                    proj = futures[future]
                    try:
                        rows = future.result()
                        project_rows[proj["project_name"]] = rows
                    except Exception as e:
                        logger.error(f"[{proj['project_name']}] FAILED: {type(e).__name__}: {e}")
                        project_rows[proj["project_name"]] = 0
                        failed_projects.append(proj["project_name"])
            except TimeoutError:
                timed_out = True
                # Identify which workers are still running
                for fut, proj in futures.items():
                    if not fut.done():
                        name = proj["project_name"]
                        logger.error(f"[{name}] TIMED OUT after {overall_timeout}s")
                        project_rows[name] = 0
                        failed_projects.append(name)
        finally:
            # shutdown(wait=False) so stuck threads don't block the pipeline
            # cancel_futures=True prevents queued tasks from starting
            executor.shutdown(wait=not timed_out, cancel_futures=True)

        # ── Project-level auto-retry (before index restore — faster writes) ──
        if failed_projects:
            def retry_once(failed_projects):
                # For failed projects: resume from cursor if available, else clean and restart
                for proj_name in failed_projects:
                    proj = next(p for p in projects if p["project_name"] == proj_name)
                    progress = extractor.db.fetchrow(
                        f'SELECT rows_written, after_ap FROM {SCHEMA_PIPELINE}.extraction_progress '
                        f'WHERE run_id = $1 AND project_did = $2',
                        str(extractor.run_id), proj["project_did"]
                    )
                    if progress and progress["after_ap"]:
                        logger.info(
                            f"[{proj_name}] Has resume point at {progress['rows_written']:,} rows "
                            f"— will resume instead of re-extracting"
                        )
                    else:
                        # No resume point — delete partial data and start fresh
                        retry_db(
                            lambda did=proj["project_did"], rid=str(extractor.run_id): extractor.db.execute(
                                f"DELETE FROM {SCHEMA_RAW}.raw_asset_tasks WHERE project_did=$1 AND run_id=$2",
                                did, rid
                            ),
                            description=f"clean partial raw data for {proj_name}"
                        )
                        retry_db(
                            lambda did=proj["project_did"], rid=str(extractor.run_id): extractor.db.execute(
                                f"DELETE FROM {SCHEMA_PIPELINE}.extraction_progress WHERE run_id=$1 AND project_did=$2",
                                rid, did
                            ),
                            description=f"clean extraction_progress for {proj_name}"
                        )

                # Retry failed projects in parallel using ThreadPoolExecutor
                retry_projects = [
                    next(p for p in projects if p["project_name"] == name)
                    for name in failed_projects
                ]
                still_failed = []
                retry_executor = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    retry_futures = {
                        retry_executor.submit(
                            extractor.extract_and_load_project,
                            proj["project_did"],
                            proj["project_name"],
                        ): proj
                        for proj in retry_projects
                    }
                    try:
                        for future in as_completed(retry_futures, timeout=overall_timeout):
                            proj = retry_futures[future]
                            try:
                                rows = future.result()
                                project_rows[proj["project_name"]] = rows
                                logger.info(f"[{proj['project_name']}] Retry SUCCEEDED: {rows:,} rows")
                            except Exception as e:
                                logger.error(f"[{proj['project_name']}] Retry FAILED: {type(e).__name__}: {e}")
                                still_failed.append(proj["project_name"])
                    except TimeoutError:
                        for fut, proj in retry_futures.items():
                            if not fut.done():
                                name = proj["project_name"]
                                logger.error(f"[{name}] Retry TIMED OUT after {overall_timeout}s")
                                still_failed.append(name)
                finally:
                    retry_executor.shutdown(wait=False, cancel_futures=True)

                return still_failed

            failed_projects = _retry_loop(retry_once, failed_projects)

        # Recalculate from project_rows — the accumulating counter may be
        # inflated by rows written in round 1 that were deleted before retry.
        total_records = sum(project_rows.values())
        extractor.total_loaded = total_records

        # Restore table: recreate indexes
        extractor.restore_table_after_load()

        # Clean up old raw data — per-project verification.
        # Successful projects get their partitions cleaned; failed projects
        # keep their old data untouched so downstream transforms don't see
        # a gap where today's extract was supposed to land.
        skipped_cleanups = extractor.clear_old_raw_data(projects, project_rows, failed_projects)

        # Clean up extraction progress tracking for this run
        retry_db(
            lambda: extractor.db.execute(
                f'DELETE FROM {SCHEMA_PIPELINE}.extraction_progress WHERE run_id = $1',
                str(extractor.run_id)
            ),
            description="clean extraction_progress"
        )

        # Row-count guard: compare successfully-extracted projects to the previous
        # successful run. Failed projects are excluded (they are already PARTIAL).
        successful_counts = {
            name: count for name, count in project_rows.items()
            if name not in failed_projects
        }
        baseline_counts, _baseline_total = extractor.get_previous_project_counts()
        abnormal_projects = detect_abnormal_counts(successful_counts, baseline_counts)

        # Compose the error_message note so the export guard blocks on partial OR
        # abnormal runs (the guard keys off a non-empty error note).
        notes = []
        if failed_projects:
            succeeded = len(project_rows) - len(failed_projects)
            notes.append(
                f"Partial extraction: {succeeded}/{len(project_rows)} projects "
                f"succeeded. Failed (old data retained): {', '.join(failed_projects)}"
            )
        if abnormal_projects:
            parts = []
            for name in abnormal_projects:
                base = baseline_counts.get(name)
                parts.append(f"{name}: {successful_counts.get(name, 0):,} (prev {base:,})"
                             if base else f"{name}: {successful_counts.get(name, 0):,} (prev n/a)")
            notes.append("Abnormal row counts vs previous run: " + "; ".join(parts))
        error_detail = " | ".join(notes) if notes else None

        # Persist counts of successful projects as next run's baseline. Status stays
        # 'success' so downstream transforms still resolve via WHERE status='success'.
        extractor.complete_pipeline_run(
            "success", total_records, error=error_detail, project_counts=successful_counts
        )

        if error_detail:
            logger.warning(f"\n{'='*60}")
            logger.warning(f"Pipeline DEGRADED: {error_detail}")
            for name, count in sorted(project_rows.items()):
                marker = ""
                if name in failed_projects:
                    marker = " [FAILED - old data retained]"
                elif name in abnormal_projects:
                    marker = " [ABNORMAL ROW COUNT]"
                logger.warning(f"  {name}: {count:,}{marker}")
            logger.warning(f"Total loaded: {total_records:,}")
            logger.warning(f"Run ID: {extractor.run_id}")
            logger.warning(f"{'='*60}\n")
        else:
            logger.info(f"\n{'='*60}")
            logger.info(f"Pipeline completed successfully")
            for name, count in sorted(project_rows.items()):
                logger.info(f"  {name}: {count:,}")
            logger.info(f"Total loaded: {total_records:,}")
            logger.info(f"Run ID: {extractor.run_id}")
            logger.info(f"{'='*60}\n")

        return PipelineOutcome(
            run_id=str(extractor.run_id),
            failed_projects=list(failed_projects),
            abnormal_projects=list(abnormal_projects),
            detail=error_detail or "",
        )

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
