#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for all GC (non-Ontel) projects.

Architecture: 12 extraction workers each write directly to DB after every
API page. Single unpartitioned data_raw.raw_asset_tasks_gc table (see spec
docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md §6
for rationale). Per-org safety check + batched cleanup DELETE.

Auto-discovery from data_staging.stg_projects with filter:
  org_name != 'Ontel' AND org_name NOT LIKE 'Testing%'
All project statuses included (in_progress, pending, complete).
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
MAX_WORKERS = 12  # 2x Ontel because GC has ~150x the project count
PROJECT_TIMEOUT_SECONDS = 600
MAX_RETRIES = 10

# Write-path indexes (dropped before bulk load, recreated after).
# The composite (org_did, run_id) index stays up across runs — used only
# for cleanup DELETE/COUNT, never on insert hot path.
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
        """All GC projects from stg_projects (auto-discovered, any status)."""
        rows = self.db.fetch(
            f"SELECT org_did, org_name, project_did, project_name, status "
            f"FROM {SCHEMA_STAGING}.stg_projects "
            f"WHERE org_name != 'Ontel' "
            f"AND org_name NOT LIKE 'Testing%' "
            f"ORDER BY org_name, project_name"
        )
        return [dict(r) for r in rows]

    def prepare_table_for_bulk_load(self):
        """Drop write-path indexes for fast bulk loading.

        The (org_did, run_id) composite index stays up — it's only used
        for cleanup DELETE/COUNT, never on the insert hot path.
        """
        logger.info("Preparing raw_asset_tasks_gc for bulk load (drop indexes)...")
        for idx_name, _ in _INDEXES:
            self.db.execute(f'DROP INDEX IF EXISTS {SCHEMA_RAW}.{idx_name}')
        logger.info("Write-path indexes dropped (org_did composite stays up)")

    def restore_table_after_load(self):
        """Recreate write-path indexes after bulk load.

        ~30-45s each on ~2M rows. Well inside the Supabase pooler ceiling
        because the index columns are narrow types (UUID + TIMESTAMPTZ).
        """
        logger.info("Restoring raw_asset_tasks_gc write-path indexes...")
        for idx_name, idx_def in _INDEXES:
            logger.info(f"  Creating {idx_name}...")
            self.db.execute(idx_def)
        logger.info("Indexes restored")

    def clear_old_raw_data(self, successful_org_dids: list):
        """Single batched DELETE for all orgs that passed extraction.

        Failed/skipped orgs (not in successful_org_dids) retain their prior
        data — same fallback as Ontel's partial-success behavior.
        """
        if not successful_org_dids:
            logger.warning("No successful orgs - skipping cleanup entirely")
            return

        run_id_str = str(self.run_id)
        logger.info(f"Cleanup: deleting old rows for {len(successful_org_dids)} orgs...")
        retry_db(
            lambda: self.db.execute(
                f'DELETE FROM {SCHEMA_RAW}.raw_asset_tasks_gc '
                f'WHERE org_did = ANY($1::text[]) AND run_id != $2',
                successful_org_dids, run_id_str
            ),
            description=f"batched cleanup of {len(successful_org_dids)} orgs"
        )
        logger.info("Cleanup complete")

    def per_org_safety_check(self, org_rows: dict) -> List[str]:
        """For each org with > 0 new rows, verify new_count >= 90% of old_count.

        Returns list of org_dids that passed the check (cleanup-eligible).
        Orgs that fail keep their old data. Orgs with 0 new rows skip cleanup
        (likely extraction failure rather than legitimate empty result).
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
                    f"old={old_count:,} (below {self.CLEANUP_ROW_THRESHOLD:.0%} threshold). "
                    f"Old data retained."
                )
                continue
            successful.append(org_did)
        return successful

    def _write_batch(self, run_id_str: str, org_did: str, project_did: str, records: list):
        """COPY a batch of rows into raw_asset_tasks_gc and verify the count."""
        expected = len(records)
        tuples = [(run_id_str, org_did, project_did, rec) for rec in records]
        result = retry_db(
            lambda: self.db.copy_records(
                "raw_asset_tasks_gc",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "org_did", "project_did", "data"],
            ),
            description="copy raw_asset_tasks_gc"
        )
        # Verify rows persisted - asyncpg returns "COPY <n>"
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

                    self._write_batch(run_id_str, org_did, project_did, rows)
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
                    break  # success — exit retry loop, continue pagination

                except requests.HTTPError as e:
                    if attempt == MAX_RETRIES - 1:
                        raise
                    backoff = 2 ** attempt
                    logger.error(
                        f"[{project_name}] Retry {attempt+1}/{MAX_RETRIES}: "
                        f"{type(e).__name__}: {e} — sleeping {backoff}s"
                    )
                    time.sleep(backoff)
                    # Re-auth in case it was a token issue
                    if resp.status_code in (401, 403):
                        headers = self.get_auth_headers()
            else:
                raise RuntimeError(
                    f"[{project_name}] Failed after {MAX_RETRIES} attempts"
                )


def run_asset_task_gc_pipeline():
    """Full GC asset_tasks pipeline: extract -> transforms (inline)."""
    extractor = AssetTaskGCExtractor()
    extractor.start_pipeline_run()
    try:
        extractor.authenticate()
        projects = extractor.get_gc_projects()
        org_count = len(set(p['org_did'] for p in projects))
        logger.info(f"\n{'='*60}")
        logger.info(f"GC Asset-Task Extraction Pipeline")
        logger.info(f"Projects: {len(projects)} across {org_count} orgs")
        logger.info(f"Workers: {MAX_WORKERS}")
        logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"{'='*60}\n")

        extractor.prepare_table_for_bulk_load()

        # Parallel extract
        org_rows = {}        # org_did -> total new rows across that org's projects
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
                    logger.error(
                        f"[{p['project_name']}] FAILED: {type(e).__name__}: {e}"
                    )
                    failed_projects.append(p["project_name"])
                    # Don't add to org_rows on failure - per-org safety check
                    # will see only the projects that DID succeed for this org.

        # Use extractor.total_loaded (updated by _write_batch on every COPY)
        # instead of sum(org_rows). org_rows misses rows from projects that
        # extracted partially before raising TimeoutError - those rows ARE in
        # the DB but the per-project counter is lost when the exception
        # propagates. Ontel uses sum(project_rows) because its project-level
        # retry deletes round-1 rows before retry, which would inflate
        # total_loaded; GC has no retry, so total_loaded is accurate.
        total_records = extractor.total_loaded

        extractor.restore_table_after_load()

        # Per-org safety check + batched cleanup
        successful_orgs = extractor.per_org_safety_check(org_rows)
        extractor.clear_old_raw_data(successful_orgs)

        # Partial-success: mirror Ontel pattern (status='success' so downstream
        # transforms find this run; detail in error_message)
        if failed_projects:
            succeeded = len(projects) - len(failed_projects)
            error_detail = (
                f"Partial extraction: {succeeded}/{len(projects)} projects succeeded. "
                f"Failed: {', '.join(failed_projects[:10])}"
                f"{'...' if len(failed_projects) > 10 else ''}"
            )
            extractor.complete_pipeline_run("success", total_records, error=error_detail)
            logger.warning(f"\nGC pipeline PARTIAL SUCCESS")
            logger.warning(f"  {succeeded}/{len(projects)} projects, {total_records:,} total rows")
            logger.warning(f"  Failed projects: {', '.join(failed_projects[:10])}")
        else:
            extractor.complete_pipeline_run("success", total_records)
            logger.info(f"\nGC pipeline complete")
            logger.info(f"  {len(projects)} projects across {org_count} orgs")
            logger.info(f"  {total_records:,} rows extracted")
            logger.info(f"  Run ID: {extractor.run_id}")

        # Inline transforms
        from transform import run_assets_gc_transform, run_asset_tasks_gc_transform
        run_assets_gc_transform(str(extractor.run_id))
        run_asset_tasks_gc_transform(str(extractor.run_id))

        return str(extractor.run_id)

    except Exception as e:
        logger.error(f"\nGC pipeline failed: {e}")
        try:
            extractor.restore_table_after_load()
        except Exception as restore_err:
            logger.error(f"Failed to restore indexes after error: {restore_err}")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    run_asset_task_gc_pipeline()
