"""
Extract asset-level info (including asset_status) from Swift API and load into
data_raw.raw_assets.

Unlike extract_asset_tasks (which hits /api/asset-tasks/_report and doesn't
return asset_status), this extractor hits /api/projects/{project_did}/assets
which returns one row per (project, asset) with a `status` field.

Scope: Ontel TECH-OPS TS13+ projects, sourced from reference.ref_ontel_techops_projects
(same as extract_asset_tasks).

Runtime: ~15-30s across all 7 projects (~32K total assets) when run in parallel.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import List, Dict

import requests

from base_extractor import BaseExtractor
from config import (
    get_logger, SCHEMA_RAW, SCHEMA_REFERENCE, MAX_RETRIES,
)

logger = get_logger("assets_extract")

PAGE_SIZE = 1000
MAX_WORKERS = 7  # one per project


class AssetsExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="assets_extract")
        self._buffer_lock = Lock()
        self._buffer: List[tuple] = []

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get the TS13+ TECH-OPS project list from the reference table."""
        rows = self.db.fetch(
            f'SELECT project_did, project_name, project_number '
            f'FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects '
            f'WHERE project_number >= $1 '
            f'ORDER BY project_number',
            min_project_number,
        )
        return [dict(r) for r in rows]

    def extract_project_assets(self, project_did: str, project_name: str) -> int:
        """Paginate /api/projects/{id}/assets and accumulate rows in self._buffer.

        Returns the number of asset rows fetched for this project.
        """
        headers = self.get_auth_headers()
        url = f"{self.base_url}/api/projects/{project_did}/assets"

        page = 0
        project_rows = 0
        start = time.monotonic()

        while True:
            params = {"page": page, "pageSize": PAGE_SIZE}

            for attempt in range(MAX_RETRIES):
                try:
                    r = requests.get(url, headers=headers, params=params, timeout=60)
                    # Retry once on 401 via re-auth
                    if r.status_code == 401:
                        headers = {"Authorization": f"Bearer {self.reauthenticate()}"}
                        r = requests.get(url, headers=headers, params=params, timeout=60)
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:
                    logger.warning(
                        f"[{project_name}] page {page} attempt {attempt+1}/{MAX_RETRIES}: "
                        f"{type(e).__name__}: {e}"
                    )
                    if attempt == MAX_RETRIES - 1:
                        raise RuntimeError(
                            f"[{project_name}] failed after {MAX_RETRIES} attempts"
                        ) from e
                    time.sleep(2 * (attempt + 1))

            rows = data.get("list", [])
            if not rows:
                break

            run_id_str = str(self.run_id)
            page_records: List[tuple] = []
            for item in rows:
                asset = item.get("asset") or {}
                page_records.append((
                    project_did,
                    asset.get("id"),              # asset_did
                    item.get("status"),            # asset_status
                    item.get("shortName"),
                    item.get("identifier"),
                    json.dumps(item),              # raw_data
                    run_id_str,
                ))

            # Dedup within this project's buffer (defensive; Swift shouldn't
            # return the same (project, asset) on two pages, but double-insert
            # would break the PK).
            with self._buffer_lock:
                self._buffer.extend(page_records)

            project_rows += len(rows)
            logger.info(
                f"[{project_name}] page {page} - "
                f"{project_rows:,} rows so far ({time.monotonic()-start:.1f}s)"
            )

            if len(rows) < PAGE_SIZE:
                break
            page += 1

        logger.info(
            f"[{project_name}] done - {project_rows:,} rows in "
            f"{time.monotonic()-start:.1f}s"
        )
        return project_rows

    def flush_to_raw(self) -> int:
        """TRUNCATE data_raw.raw_assets and bulk insert everything in self._buffer."""
        if not self._buffer:
            logger.warning("no rows to flush")
            return 0

        # Dedup by PK (project_did, asset_did) -- last wins.
        dedup = {}
        for row in self._buffer:
            dedup[(row[0], row[1])] = row
        records = list(dedup.values())

        logger.info(
            f"flushing {len(records):,} unique (project, asset) rows to "
            f"{SCHEMA_RAW}.raw_assets (dedup removed {len(self._buffer) - len(records):,})"
        )

        # TRUNCATE + fresh load each run. No history retention needed.
        self.db.execute(f'TRUNCATE {SCHEMA_RAW}.raw_assets')

        # Bulk insert via executemany (JSONB handling works with COPY too,
        # but executemany is simpler for a 32K-row batch).
        batch_size = 5000
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            self.db.executemany(
                f'INSERT INTO {SCHEMA_RAW}.raw_assets '
                f'(project_did, asset_did, asset_status, asset_short_name, '
                f'asset_identifier, raw_data, run_id) '
                f'VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)',
                batch,
            )

        return len(records)


def run_assets_extract():
    """Entry point: extract + load. Called from main.py / pipeline.py."""
    extractor = AssetsExtractor()
    try:
        extractor.start_pipeline_run()

        projects = extractor.get_project_dids(min_project_number=13)
        logger.info(f"Extracting assets for {len(projects)} TECH-OPS projects")

        total_rows = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    extractor.extract_project_assets,
                    p["project_did"],
                    p["project_name"],
                ): p
                for p in projects
            }
            for fut in as_completed(futures, timeout=600):
                total_rows += fut.result()

        loaded = extractor.flush_to_raw()
        extractor.complete_pipeline_run(status="success", records=loaded)
        logger.info(f"Assets extract complete: {loaded:,} rows in raw_assets")
        return str(extractor.run_id)

    except Exception as e:
        logger.exception("assets extract failed")
        extractor.complete_pipeline_run(status="failed", error=str(e))
        raise


if __name__ == "__main__":
    run_assets_extract()
