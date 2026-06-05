#!/usr/bin/env python3
"""Targeted Asset-Task extraction for report-driven data needs.

Lighter alternative to extract_asset_tasks_gc.py for use cases where:
  - Only a known list of (org, project) tuples is needed (configured in
    `reference.report_targets`)
  - Flat task-level rows are sufficient (no nested asset/requirement JSONB)
  - Snapshot reload semantics are desired (each run wipes and refills
    the rows for that report_name)

Uses the lighter Swift API walk that the standalone Asset Export tool
follows:
    /api/projects/{p}/assets             → asset metadata
    /api/asset-projects/{a}/asset-tasks  → flat task list

Per-row payload is ~10x smaller than the /assets/_export endpoint,
making this fast enough (~3-5 min for ~5K assets / ~110K tasks) to run
on demand rather than as a scheduled nightly.

CLI:
    python main.py --pipeline targeted_asset_tasks [--report-name X]

Env vars (optional):
    REPORT_NAME — override default "all enabled reports" filter
"""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date
from typing import Dict, List, Optional, Tuple

import requests

from base_extractor import BaseExtractor
from db import retry_db

_ET = timezone.utc  # Postgres stores DATE without TZ; we just want the calendar day


def _epoch_ms_to_date(epoch_ms) -> Optional[date]:
    """Convert Swift's `approvedOn` epoch-ms field to a Python date.
    Returns None for null / non-numeric input.
    """
    if epoch_ms in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=_ET).date()
    except (TypeError, ValueError):
        return None

logger = logging.getLogger("pipeline.targeted_asset_tasks")

SCHEMA_REFERENCE = "reference"
SCHEMA_STAGING = "data_staging"

PAGE_SIZE = 1000
ASSET_WORKERS = 8           # parallel task-fetches across all assets
PROJECT_WORKERS = 4         # parallel asset-list fetches across projects
MAX_RETRIES = 3
HTTP_TIMEOUT_SECS = 60

BASE_URL = "https://prod.api.swiftprojects.io"


class TargetedAssetTasksExtractor(BaseExtractor):
    def __init__(self):
        super().__init__("targeted_asset_tasks")

    # ----- API helpers -----

    def _get_json(self, url: str, params: Dict, attempt_label: str) -> Dict:
        """Safe GET → JSON with retry. Returns {} on persistent failure."""
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
                        f"{attempt_label}: non-JSON response (ctype={ctype}); body[:200]={resp.text[:200]!r}"
                    )
                    return {}
                return resp.json()
            except requests.HTTPError as e:
                logger.warning(
                    f"{attempt_label}: HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES}): {e}"
                )
            except Exception as e:
                logger.warning(
                    f"{attempt_label}: {type(e).__name__}: {e} (attempt {attempt}/{MAX_RETRIES})"
                )
            time.sleep(1 * attempt)  # linear backoff
        return {}

    def fetch_assets(self, project_did: str, project_name: str) -> List[Dict]:
        """List assets for a project (paginated)."""
        url = f"{BASE_URL}/api/projects/{project_did}/assets"
        results: List[Dict] = []
        page = 0
        while True:
            data = self._get_json(
                url,
                params={"page": page, "pageSize": PAGE_SIZE},
                attempt_label=f"[{project_name}] assets page {page}",
            )
            rows = data.get("list", [])
            if not rows:
                break
            for item in rows:
                asset = item.get("asset") or {}
                results.append({
                    "asset_project_did": item.get("id"),
                    "asset_id": asset.get("id"),
                    "asset_identifier": item.get("identifier"),
                    "asset_name": item.get("shortName"),
                    "asset_status": item.get("status"),
                })
            if len(rows) < PAGE_SIZE:
                break
            page += 1
        return results

    def fetch_tasks(self, asset_project_did: str) -> List[Dict]:
        """List tasks for an asset-project (paginated)."""
        url = f"{BASE_URL}/api/asset-projects/{asset_project_did}/asset-tasks"
        results: List[Dict] = []
        page = 0
        while True:
            data = self._get_json(
                url,
                params={
                    "page": page,
                    "pageSize": PAGE_SIZE,
                    "timezone": "America/New_York",
                    "dateFormat": "yyyy-MM-dd'T'HH:mm:ssZ",
                },
                attempt_label=f"[ap={asset_project_did}] tasks page {page}",
            )
            rows = data.get("list", [])
            if not rows:
                break
            for item in rows:
                if item.get("collection") != "asset-tasks":
                    continue
                assigned = item.get("assignedTo") or {}
                task_id = item.get("id")
                results.append({
                    "task_did": task_id,
                    "task_name": item.get("name"),
                    "task_status": item.get("status"),
                    "assigned_to": assigned.get("name"),
                    "task_description": item.get("description"),
                    "task_url": f"https://swiftprojects.io/#/app/assets/tasks/{task_id}/requirements",
                    "task_approved_on": _epoch_ms_to_date(item.get("approvedOn")),
                    "task_submitted_on": _epoch_ms_to_date(item.get("submittedOn")),
                })
            if len(rows) < PAGE_SIZE:
                break
            page += 1
        return results

    # ----- Targets / config -----

    def get_targets(self, report_name: Optional[str] = None) -> List[Dict]:
        """Read enabled rows from reference.report_targets."""
        sql = (
            f"SELECT report_name, org_did, org_name, project_did, project_name "
            f"FROM {SCHEMA_REFERENCE}.report_targets WHERE enabled"
        )
        params: list = []
        if report_name:
            sql += " AND report_name = $1"
            params.append(report_name)
        sql += " ORDER BY report_name, project_name"
        rows = retry_db(
            lambda: self.db.fetch(sql, *params),
            description="fetch report_targets",
        )
        return [dict(r) for r in rows]

    # ----- Write path -----

    def _delete_report_rows(self, report_name: str) -> int:
        """Snapshot semantics: wipe this report's rows before reloading."""
        result = retry_db(
            lambda: self.db.execute(
                f"DELETE FROM {SCHEMA_STAGING}.stg_targeted_asset_tasks "
                f"WHERE report_name = $1",
                report_name,
            ),
            description=f"delete prior rows for report_name={report_name}",
        )
        # asyncpg returns "DELETE <n>"
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    def _insert_task_rows(self, rows: List[Tuple]) -> None:
        """Bulk insert via COPY."""
        if not rows:
            return
        retry_db(
            lambda: self.db.copy_records(
                "stg_targeted_asset_tasks",
                schema_name=SCHEMA_STAGING,
                records=rows,
                columns=[
                    "report_name", "run_id", "org_did", "org_name",
                    "project_did", "project_name", "project_status",
                    "asset_project_did", "asset_id", "asset_identifier",
                    "asset_name", "asset_status",
                    "task_did", "task_name", "task_status",
                    "assigned_to", "task_description", "task_url",
                    "task_approved_on", "task_submitted_on",
                ],
            ),
            description=f"copy {len(rows)} task rows",
        )


def run_targeted_asset_tasks_pipeline(report_name: Optional[str] = None) -> bool:
    """Orchestrate: get targets → fetch assets → fetch tasks (parallel) → write."""
    extractor = TargetedAssetTasksExtractor()
    extractor.start_pipeline_run()

    try:
        # 1. Load targets from config
        targets = extractor.get_targets(report_name=report_name)
        if not targets:
            logger.warning(
                f"No enabled targets found"
                + (f" for report_name={report_name}" if report_name else " (all reports)")
            )
            extractor.complete_pipeline_run("success", records=0)
            return True

        report_names_in_scope = sorted({t["report_name"] for t in targets})
        logger.info(
            f"Loaded {len(targets)} targets across {len(report_names_in_scope)} report(s): "
            f"{', '.join(report_names_in_scope)}"
        )

        # 2. Wipe prior snapshots (per report_name in scope)
        for rn in report_names_in_scope:
            deleted = extractor._delete_report_rows(rn)
            logger.info(f"[{rn}] Deleted {deleted:,} prior rows (snapshot reload)")

        # 3. Fetch asset lists in parallel (per project)
        logger.info(f"Fetching assets for {len(targets)} projects (workers={PROJECT_WORKERS})...")
        # Each entry: (target_dict, [assets])
        project_assets: List[Tuple[Dict, List[Dict]]] = []
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=PROJECT_WORKERS) as pool:
            futures = {
                pool.submit(extractor.fetch_assets, t["project_did"], t["project_name"]): t
                for t in targets
            }
            for fut in as_completed(futures):
                target = futures[fut]
                try:
                    assets = fut.result()
                except Exception as e:
                    logger.error(f"[{target['project_name']}] asset fetch failed: {e}")
                    assets = []
                project_assets.append((target, assets))
                logger.info(f"[{target['project_name']}] {len(assets):,} assets")

        total_assets = sum(len(a) for _, a in project_assets)
        logger.info(f"Total assets across all projects: {total_assets:,} (elapsed {time.monotonic()-t0:.1f}s)")

        # 4. Build (target, asset) jobs and fetch tasks in parallel
        # Each job is one HTTP call to /asset-projects/{ap}/asset-tasks.
        logger.info(f"Fetching tasks across {total_assets:,} assets (workers={ASSET_WORKERS})...")
        all_rows: List[Tuple] = []
        completed_count = 0
        t1 = time.monotonic()
        with ThreadPoolExecutor(max_workers=ASSET_WORKERS) as pool:
            futures = {}
            for target, assets in project_assets:
                for asset in assets:
                    apd = asset["asset_project_did"]
                    if not apd:
                        continue
                    futures[pool.submit(extractor.fetch_tasks, apd)] = (target, asset)

            for fut in as_completed(futures):
                target, asset = futures[fut]
                completed_count += 1
                try:
                    tasks = fut.result()
                except Exception as e:
                    logger.error(
                        f"[{target['project_name']} / {asset.get('asset_name')}] "
                        f"task fetch failed: {e}"
                    )
                    tasks = []

                for task in tasks:
                    all_rows.append((
                        target["report_name"],
                        extractor.run_id,
                        target["org_did"],
                        target["org_name"],
                        target["project_did"],
                        target["project_name"],
                        None,  # project_status — not fetched here; can be backfilled later
                        asset["asset_project_did"],
                        asset["asset_id"],
                        asset["asset_identifier"],
                        asset["asset_name"],
                        asset["asset_status"],
                        task["task_did"],
                        task["task_name"],
                        task["task_status"],
                        task["assigned_to"],
                        task["task_description"],
                        task["task_url"],
                        task.get("task_approved_on"),
                        task.get("task_submitted_on"),
                    ))

                # Periodic progress log every 500 assets
                if completed_count % 500 == 0:
                    elapsed = time.monotonic() - t1
                    rate = completed_count / elapsed if elapsed > 0 else 0
                    eta = (total_assets - completed_count) / rate if rate > 0 else 0
                    logger.info(
                        f"Progress: {completed_count:,}/{total_assets:,} assets "
                        f"({rate:.1f}/s, ETA {eta:.0f}s); {len(all_rows):,} tasks collected"
                    )

        logger.info(
            f"All tasks fetched: {len(all_rows):,} rows in {time.monotonic()-t1:.1f}s"
        )

        # 5. Bulk write
        logger.info(f"Writing {len(all_rows):,} rows to stg_targeted_asset_tasks...")
        t2 = time.monotonic()
        extractor._insert_task_rows(all_rows)
        logger.info(f"Write complete in {time.monotonic()-t2:.1f}s")

        # 6. Per-report summary
        for rn in report_names_in_scope:
            count = sum(1 for r in all_rows if r[0] == rn)
            logger.info(f"[{rn}] {count:,} task rows written")

        extractor.complete_pipeline_run("success", records=len(all_rows))
        return True

    except Exception as e:
        logger.error(f"Targeted extraction failed: {type(e).__name__}: {e}")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise
