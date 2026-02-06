#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for specified projects
Uses ThreadPoolExecutor for parallel extraction
"""

import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Event
from datetime import datetime, timezone
from typing import List, Dict, Optional
from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_REFERENCE, get_logger
)
from base_extractor import BaseExtractor

logger = get_logger("asset_tasks")

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 3  # Concurrent API threads
LOAD_BATCH_SIZE = 500


class AssetTaskExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="asset_tasks_extract")

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get project DIDs from reference table"""
        result = self.client.schema(SCHEMA_REFERENCE).table("ref_ontel_techops_projects").select(
            "project_did, project_name, project_number"
        ).gte("project_number", min_project_number).order("project_number").execute()

        return result.data

    def extract_project_asset_tasks(
        self,
        project_did: str,
        project_name: str,
        result_queue: Queue
    ) -> int:
        """Extract all asset-tasks for a single project, streaming batches to queue"""
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
                        return project_rows

                    resp.raise_for_status()
                    data = resp.json().get("list", [])

                    if not data:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
                        return project_rows

                    # Stream batch to queue immediately
                    result_queue.put((project_did, data))
                    project_rows += len(data)
                    page_count += 1

                    if page_count % 50 == 0:
                        logger.info(f"[{project_name}] Page {page_count} - {project_rows:,} rows")

                    # Handle keyset pagination
                    next_info = resp.json().get("next")
                    if not next_info:
                        logger.info(f"[{project_name}] Complete - {project_rows:,} rows")
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

    def load_batch(self, project_did: str, batch: List[Dict]):
        """Load a batch of asset-tasks to raw table"""
        rows = [
            {
                "run_id": str(self.run_id),
                "project_did": project_did,
                "data": asset
            }
            for asset in batch
        ]

        self.client.schema(SCHEMA_RAW).table("raw_asset_tasks").insert(rows).execute()
        self.increment_loaded(len(batch))

    def loader_worker(self, result_queue: Queue, stop_event):
        """Background worker that loads batches from queue to database"""
        from queue import Empty
        pending_batches = {}  # project_did -> list of records

        while True:
            try:
                # Get batch from queue with timeout
                project_did, data = result_queue.get(timeout=0.5)

                # Accumulate batches
                if project_did not in pending_batches:
                    pending_batches[project_did] = []
                pending_batches[project_did].extend(data)

                # Load when batch is large enough
                while len(pending_batches[project_did]) >= LOAD_BATCH_SIZE:
                    batch = pending_batches[project_did][:LOAD_BATCH_SIZE]
                    pending_batches[project_did] = pending_batches[project_did][LOAD_BATCH_SIZE:]
                    self.load_batch(project_did, batch)

                result_queue.task_done()

            except Empty:
                # Check if we should exit
                if stop_event.is_set() and result_queue.empty():
                    break
            except Exception as e:
                logger.error(f"Loader error: {e}")
                result_queue.task_done()

        # Load all remaining data
        logger.info("Flushing remaining data...")
        for project_did, data in pending_batches.items():
            if data:
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(project_did, batch)
        logger.info("Loader complete")

    def batch_delete_table(self, schema: str, table: str):
        """Delete all rows from a table in batches to avoid memory issues"""
        # Get ID range
        min_result = self.client.schema(schema).table(table).select('id').order('id').limit(1).execute()
        max_result = self.client.schema(schema).table(table).select('id').order('id', desc=True).limit(1).execute()

        if not min_result.data or not max_result.data:
            return  # Table is empty

        min_id = min_result.data[0]['id']
        max_id = max_result.data[0]['id']

        batch_size = 50000
        current_id = min_id

        while current_id <= max_id:
            end_id = current_id + batch_size
            self.client.schema(schema).table(table).delete().gte('id', current_id).lt('id', end_id).execute()
            current_id = end_id

    def clear_old_raw_data(self):
        """Clear old raw data after successful extraction (keep current run_id)."""
        logger.info(f"Cleaning up old raw data (keeping run_id={self.run_id})...")
        self.client.schema(SCHEMA_RAW).table("raw_asset_tasks").delete().neq(
            "run_id", str(self.run_id)
        ).execute()
        logger.info("Old raw data cleaned up")

    # start_pipeline_run() and complete_pipeline_run() inherited from BaseExtractor


def run_asset_task_pipeline(min_project_number: int = 13, max_workers: int = MAX_WORKERS):
    """Main pipeline for extracting asset-tasks with parallel processing"""
    logger.info(f"\n{'='*60}")
    logger.info(f"Asset-Task Extraction Pipeline (Parallel)")
    logger.info(f"Projects: TECH-OPS TS{min_project_number}+")
    logger.info(f"Workers: {max_workers}")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    extractor = AssetTaskExtractor()

    try:
        extractor.start_pipeline_run()
        extractor.authenticate()

        # Get projects from reference table
        projects = extractor.get_project_dids(min_project_number)
        logger.info(f"Found {len(projects)} projects to process\n")

        # Create queue for results
        result_queue = Queue()

        # Create stop event for loader
        from threading import Event
        stop_event = Event()

        # Start background loader thread
        loader_thread = Thread(
            target=extractor.loader_worker,
            args=(result_queue, stop_event),
            daemon=True
        )
        loader_thread.start()

        # Extract projects in parallel
        project_rows = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    extractor.extract_project_asset_tasks,
                    proj["project_did"],
                    proj["project_name"],
                    result_queue
                ): proj
                for proj in projects
            }

            for future in as_completed(futures):
                proj = futures[future]
                try:
                    rows = future.result()
                    project_rows[proj["project_name"]] = rows
                except Exception as e:
                    logger.error(f"[{proj['project_name']}] FAILED: {e}")
                    project_rows[proj["project_name"]] = 0

        # Wait for queue to be fully processed first
        logger.info("Waiting for loader to finish...")
        result_queue.join()

        # Signal loader to stop and wait for it
        stop_event.set()
        loader_thread.join(timeout=120)

        total_records = extractor.total_loaded

        # Clean up old raw data now that new extraction succeeded
        extractor.clear_old_raw_data()

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
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract asset-tasks from Swift API")
    parser.add_argument("--min-project", type=int, default=13, help="Minimum project number (default: 13)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    args = parser.parse_args()

    run_asset_task_pipeline(min_project_number=args.min_project, max_workers=args.workers)
