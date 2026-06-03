#!/usr/bin/env python3
"""Requirement-level extraction for targeted reports (e.g. Open Items Report).

Companion to extract_targeted_asset_tasks. For each report scope defined
in `reference.report_targets`, this pipeline:

  1. Reads `data_staging.stg_user_priorities` rows that match the scope:
       org_did, project_did IN <report's enabled targets>
       AND task_name ILIKE '%punch%'
       AND status IN ('pending', 'in_progress')
       AND assigned_to IS NOT NULL
  2. For each task_did, fetches requirements via
       GET /api/asset-tasks/{task_did}/requirements
     (filters to requirement_status IN ('pending', 'in_progress') at
     extract time)
  3. Writes results to `data_staging.stg_targeted_task_requirements`
     with snapshot-reload semantics per report_name.

CLI:
    python main.py --pipeline targeted_task_requirements [REPORT_NAME=X]

Env vars:
    REPORT_NAME — optional filter to one report scope
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import requests

from base_extractor import BaseExtractor
from db import retry_db

logger = logging.getLogger("pipeline.targeted_task_requirements")

SCHEMA_REFERENCE = "reference"
SCHEMA_STAGING = "data_staging"

PAGE_SIZE = 1000
WORKERS = 8
MAX_RETRIES = 3
HTTP_TIMEOUT_SECS = 60

BASE_URL = "https://prod.api.swiftprojects.io"

# Statuses we keep at extract time. Requirements outside this set are
# considered "closed" / "not actionable" and filtered out.
OPEN_REQ_STATUSES = {"pending", "in_progress"}


def _coerce_text(v):
    """Swift API occasionally returns bool for description fields. asyncpg
    COPY expects str or None for TEXT columns, so coerce anything non-string
    to its str form (or None for null-ish values)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return str(v)


class TargetedTaskRequirementsExtractor(BaseExtractor):
    def __init__(self):
        super().__init__("targeted_task_requirements")

    def _get_json(self, url: str, params: Dict, attempt_label: str) -> Dict:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url,
                    headers=self.get_auth_headers(),
                    params=params,
                    timeout=HTTP_TIMEOUT_SECS,
                )
                if resp.status_code == 401 and attempt == 1:
                    self.reauthenticate()
                    continue
                resp.raise_for_status()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" not in ctype:
                    logger.warning(
                        f"{attempt_label}: non-JSON response (ctype={ctype})"
                    )
                    return {}
                return resp.json()
            except requests.HTTPError as e:
                logger.warning(
                    f"{attempt_label}: HTTP error attempt {attempt}/{MAX_RETRIES}: {e}"
                )
            except Exception as e:
                logger.warning(
                    f"{attempt_label}: {type(e).__name__}: {e} (attempt {attempt}/{MAX_RETRIES})"
                )
            time.sleep(1 * attempt)
        return {}

    def fetch_requirements(self, task_did: str) -> List[Dict]:
        """List requirements for one asset-task, filtered to open statuses."""
        url = f"{BASE_URL}/api/asset-tasks/{task_did}/requirements"
        results: List[Dict] = []
        page = 0
        while True:
            data = self._get_json(
                url,
                params={"page": page, "pageSize": PAGE_SIZE},
                attempt_label=f"[task={task_did}] requirements page {page}",
            )
            rows = data.get("list", [])
            if not rows:
                break
            for r in rows:
                status = (r.get("status") or "").lower()
                if status not in OPEN_REQ_STATUSES:
                    continue
                asset_task = r.get("assetTask") or {}
                assigned = (asset_task.get("assignedTo") or {})
                results.append({
                    "requirement_name": _coerce_text(r.get("name")),
                    "requirement_status": _coerce_text(r.get("status")),
                    "requirement_description": _coerce_text(r.get("description")),
                    "requirement_assigned_to": _coerce_text(assigned.get("name")),
                })
            if len(rows) < PAGE_SIZE:
                break
            page += 1
        return results

    def get_tasks_for_report(self, report_name: str) -> List[Dict]:
        """Pull the task_dids in scope for the given report from stg_user_priorities."""
        sql = f"""
        SELECT
          up.task_did,
          up.task_name,
          up.asset_name,
          up.project,
          up.org_did,
          up.project_did,
          up.assigned_to AS task_assigned_to,
          up.status AS task_status
        FROM {SCHEMA_STAGING}.stg_user_priorities up
        JOIN {SCHEMA_REFERENCE}.report_targets rt
          ON rt.report_name = $1
         AND rt.enabled
         AND rt.org_did = up.org_did
         AND rt.project_did = up.project_did
        WHERE up.task_name ILIKE '%punch%'
          AND up.status IN ('pending', 'in_progress')
          AND up.assigned_to IS NOT NULL
        ORDER BY up.project, up.asset_name, up.task_name;
        """
        rows = retry_db(
            lambda: self.db.fetch(sql, report_name),
            description=f"fetch task scope for report_name={report_name}",
        )
        return [dict(r) for r in rows]

    def get_distinct_report_names(self) -> List[str]:
        """Discover all report_names with at least one enabled target."""
        sql = f"""
        SELECT DISTINCT report_name
        FROM {SCHEMA_REFERENCE}.report_targets
        WHERE enabled
        ORDER BY report_name;
        """
        rows = retry_db(
            lambda: self.db.fetch(sql),
            description="list enabled report_names",
        )
        return [r["report_name"] for r in rows]

    def _delete_report_rows(self, report_name: str) -> int:
        result = retry_db(
            lambda: self.db.execute(
                f"DELETE FROM {SCHEMA_STAGING}.stg_targeted_task_requirements "
                f"WHERE report_name = $1",
                report_name,
            ),
            description=f"delete prior rows for report_name={report_name}",
        )
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    def _insert_rows(self, rows: List[Tuple]) -> None:
        if not rows:
            return
        retry_db(
            lambda: self.db.copy_records(
                "stg_targeted_task_requirements",
                schema_name=SCHEMA_STAGING,
                records=rows,
                columns=[
                    "report_name", "run_id", "task_did",
                    "requirement_name", "requirement_status",
                    "requirement_description", "requirement_assigned_to",
                ],
            ),
            description=f"copy {len(rows)} requirement rows",
        )


def run_targeted_task_requirements_pipeline(report_name: Optional[str] = None) -> bool:
    extractor = TargetedTaskRequirementsExtractor()
    extractor.start_pipeline_run()

    try:
        if report_name:
            report_names = [report_name]
        else:
            report_names = extractor.get_distinct_report_names()

        if not report_names:
            logger.warning("No enabled report_names found")
            extractor.complete_pipeline_run("success", records=0)
            return True

        total_rows_written = 0

        for rn in report_names:
            logger.info(f"\n=== Processing report_name={rn} ===")
            tasks = extractor.get_tasks_for_report(rn)
            logger.info(f"[{rn}] {len(tasks)} tasks in scope (from stg_user_priorities)")

            if not tasks:
                # Still wipe any old rows even if scope is empty now
                deleted = extractor._delete_report_rows(rn)
                logger.info(f"[{rn}] Deleted {deleted:,} prior rows (no current scope)")
                continue

            # Snapshot reload: wipe prior, then fill
            deleted = extractor._delete_report_rows(rn)
            logger.info(f"[{rn}] Deleted {deleted:,} prior rows (snapshot reload)")

            # Fetch requirements per task in parallel
            t0 = time.monotonic()
            row_tuples: List[Tuple] = []
            completed = 0
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {
                    pool.submit(extractor.fetch_requirements, t["task_did"]): t
                    for t in tasks
                }
                for fut in as_completed(futures):
                    t = futures[fut]
                    completed += 1
                    try:
                        reqs = fut.result()
                    except Exception as e:
                        logger.error(
                            f"[{t['project']} / {t['asset_name']} / {t['task_name']}] "
                            f"requirement fetch failed: {e}"
                        )
                        reqs = []

                    for req in reqs:
                        row_tuples.append((
                            rn,
                            extractor.run_id,
                            t["task_did"],
                            req["requirement_name"],
                            req["requirement_status"],
                            req["requirement_description"],
                            req["requirement_assigned_to"],
                        ))

                    if completed % 50 == 0:
                        logger.info(
                            f"[{rn}] {completed}/{len(tasks)} tasks fetched, "
                            f"{len(row_tuples):,} reqs collected"
                        )

            logger.info(
                f"[{rn}] All tasks fetched: {len(row_tuples):,} requirements in "
                f"{time.monotonic()-t0:.1f}s"
            )

            extractor._insert_rows(row_tuples)
            logger.info(f"[{rn}] Wrote {len(row_tuples):,} requirement rows")
            total_rows_written += len(row_tuples)

        extractor.complete_pipeline_run("success", records=total_rows_written)
        return True

    except Exception as e:
        logger.error(f"Targeted requirement extraction failed: {type(e).__name__}: {e}")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise
