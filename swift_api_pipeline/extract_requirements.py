#!/usr/bin/env python3
"""
Extract Asset Task Requirements from Swift API
Fetches requirements for each task via /api/asset-tasks/{task_DID}/requirements
Processes by project to allow incremental extraction
"""

import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Lock, Event
from datetime import datetime, timezone
from typing import List, Dict
from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_REFERENCE, get_logger, retry_supabase
)
from base_extractor import BaseExtractor

logger = get_logger("requirements")

PAGE_SIZE = 1000
MAX_RETRIES = 5
MAX_WORKERS = 50  # Optimal based on benchmarking (16.5 tasks/s at 50 workers)
LOAD_BATCH_SIZE = 500
TASK_BATCH_SIZE = 100  # How many tasks to process before logging progress


def _queue_join_with_timeout(q, timeout):
    """Like Queue.join() but with a timeout to prevent deadlocks."""
    with q.all_tasks_done:
        endtime = time.monotonic() + timeout
        while q.unfinished_tasks:
            remaining = endtime - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Queue.join() timed out after {timeout}s "
                    f"({q.unfinished_tasks} unfinished tasks) - possible deadlock"
                )
            q.all_tasks_done.wait(remaining)


class RequirementsExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="requirements_extract")
        self.total_tasks_processed = 0
        self.total_tasks_with_requirements = 0
        self.stats_lock = Lock()

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get project DIDs from reference table"""
        result = self.client.schema(SCHEMA_REFERENCE).table("ref_ontel_techops_projects").select(
            "project_did, project_name, project_number"
        ).gte("project_number", min_project_number).order("project_number").execute()

        return result.data

    def get_tasks_for_project(self, project_did: str) -> List[Dict]:
        """Get all task DIDs for a project from staging table"""
        tasks = []
        batch_size = 1000  # Supabase REST API has a 1000 row limit by default
        offset = 0

        while True:
            result = self.client.schema(SCHEMA_STAGING).table("stg_asset_tasks").select(
                "task_did, asset_did"
            ).eq("project_did", project_did).range(offset, offset + batch_size - 1).execute()

            if not result.data:
                break

            tasks.extend(result.data)
            offset += batch_size

            if len(result.data) < batch_size:
                break

        return tasks

    def extract_task_requirements(
        self,
        project_did: str,
        task_did: str,
        asset_did: str,
        result_queue: Queue
    ) -> int:
        """Extract all requirements for a single task"""
        if not self.token:
            self.authenticate()

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/asset-tasks/{task_did}/requirements"

        page = 0
        total_rows = 0

        while True:
            params = {"page": page, "pageSize": PAGE_SIZE}

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=30)

                    if resp.status_code == 204:
                        return total_rows

                    if resp.status_code == 404:
                        # Task not found - skip
                        return 0

                    resp.raise_for_status()
                    data = resp.json()
                    rows = data.get("list", [])

                    if not rows:
                        return total_rows

                    # Add context to each requirement
                    for row in rows:
                        row["_project_did"] = project_did
                        row["_task_did"] = task_did
                        row["_asset_did"] = asset_did

                    # Stream batch to queue
                    result_queue.put((project_did, task_did, rows))
                    total_rows += len(rows)

                    # Check for next page
                    if len(rows) < PAGE_SIZE:
                        return total_rows

                    page += 1
                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 10)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        self.reauthenticate()
                        headers = {"Authorization": f"Bearer {self.token}"}
                    else:
                        time.sleep(wait_time)
            else:
                # Failed after all retries
                return total_rows

        return total_rows

    def process_task_batch(
        self,
        project_did: str,
        tasks: List[Dict],
        result_queue: Queue
    ) -> Dict:
        """Process a batch of tasks in parallel"""
        results = {"processed": 0, "with_requirements": 0, "total_requirements": 0}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    self.extract_task_requirements,
                    project_did,
                    task["task_did"],
                    task["asset_did"],
                    result_queue
                ): task
                for task in tasks
            }

            for future in as_completed(futures):
                try:
                    req_count = future.result()
                    results["processed"] += 1
                    if req_count > 0:
                        results["with_requirements"] += 1
                        results["total_requirements"] += req_count
                except Exception as e:
                    results["processed"] += 1

        return results

    def load_batch(self, project_did: str, task_did: str, batch: List[Dict]):
        """Load a batch of requirements to raw table"""
        rows = [
            {
                "run_id": str(self.run_id),
                "project_did": project_did,
                "task_did": task_did,
                "data": record
            }
            for record in batch
        ]

        retry_supabase(
            lambda: self.client.schema(SCHEMA_RAW).table("raw_asset_task_requirements").insert(rows).execute(),
            description="insert raw_asset_task_requirements"
        )
        self.increment_loaded(len(batch))

    def loader_worker(self, result_queue: Queue, stop_event: Event):
        """Background worker that loads batches from queue to database"""
        from queue import Empty
        pending_batches = {}  # (project_did, task_did) -> list of records

        while True:
            try:
                project_did, task_did, data = result_queue.get(timeout=0.5)

                key = (project_did, task_did)
                if key not in pending_batches:
                    pending_batches[key] = []
                pending_batches[key].extend(data)

                # Load when batch is large enough
                while len(pending_batches[key]) >= LOAD_BATCH_SIZE:
                    batch = pending_batches[key][:LOAD_BATCH_SIZE]
                    pending_batches[key] = pending_batches[key][LOAD_BATCH_SIZE:]
                    self.load_batch(project_did, task_did, batch)

                result_queue.task_done()

            except Empty:
                if stop_event.is_set() and result_queue.empty():
                    break
            except Exception as e:
                logger.error(f"Loader error: {e}")
                result_queue.task_done()

        # Load all remaining data
        for (project_did, task_did), data in pending_batches.items():
            if data:
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(project_did, task_did, batch)

    def extract_project_requirements(
        self,
        project_did: str,
        project_name: str,
        result_queue: Queue
    ) -> Dict:
        """Extract all requirements for a project"""
        logger.info(f"[{project_name}] Getting tasks from staging...")

        tasks = self.get_tasks_for_project(project_did)
        total_tasks = len(tasks)

        if not tasks:
            logger.info(f"[{project_name}] No tasks found")
            return {"tasks": 0, "with_requirements": 0, "requirements": 0}

        logger.info(f"[{project_name}] Processing {total_tasks:,} tasks...")

        total_processed = 0
        total_with_requirements = 0
        total_requirements = 0

        # Process tasks in batches for better progress reporting
        for i in range(0, len(tasks), TASK_BATCH_SIZE):
            batch = tasks[i:i + TASK_BATCH_SIZE]
            results = self.process_task_batch(project_did, batch, result_queue)

            total_processed += results["processed"]
            total_with_requirements += results["with_requirements"]
            total_requirements += results["total_requirements"]

            # Log progress every 1000 tasks
            if total_processed % 1000 == 0 or total_processed == total_tasks:
                pct = (total_processed / total_tasks) * 100
                logger.info(f"[{project_name}] {total_processed:,}/{total_tasks:,} tasks ({pct:.1f}%) - {total_requirements:,} requirements")

        logger.info(f"[{project_name}] Complete - {total_tasks:,} tasks, {total_with_requirements:,} with requirements, {total_requirements:,} total requirements")

        return {
            "tasks": total_tasks,
            "with_requirements": total_with_requirements,
            "requirements": total_requirements
        }

    # start_pipeline_run() and complete_pipeline_run() inherited from BaseExtractor


def run_requirements_pipeline(
    min_project_number: int = 13,
    project_numbers: List[int] = None,
    max_workers: int = MAX_WORKERS
):
    """
    Main pipeline for extracting requirements data.

    Args:
        min_project_number: Minimum project number to process (default: 13)
        project_numbers: Specific project numbers to process (overrides min_project_number)
        max_workers: Number of parallel workers for API calls
    """
    global MAX_WORKERS
    MAX_WORKERS = max_workers

    logger.info(f"{'='*60}")
    logger.info(f"Requirements Extraction Pipeline")
    if project_numbers:
        logger.info(f"Projects: TS{project_numbers}")
    else:
        logger.info(f"Projects: TECH-OPS TS{min_project_number}+")
    logger.info(f"Workers: {max_workers}")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}")

    extractor = RequirementsExtractor()

    try:
        extractor.start_pipeline_run()
        extractor.authenticate()

        # Get projects from reference table
        all_projects = extractor.get_project_dids(min_project_number)

        # Filter to specific projects if requested
        if project_numbers:
            projects = [p for p in all_projects if p["project_number"] in project_numbers]
        else:
            projects = all_projects

        logger.info(f"Processing {len(projects)} projects")

        # Create queue for results
        result_queue = Queue()
        stop_event = Event()

        # Start background loader thread
        loader_thread = Thread(
            target=extractor.loader_worker,
            args=(result_queue, stop_event),
            daemon=True
        )
        loader_thread.start()

        # Process each project sequentially (parallelism is within each project)
        project_stats = {}
        for proj in projects:
            stats = extractor.extract_project_requirements(
                proj["project_did"],
                proj["project_name"],
                result_queue
            )
            project_stats[proj["project_name"]] = stats

        # Wait for queue to be fully processed
        logger.info("Waiting for loader to finish...")
        _queue_join_with_timeout(result_queue, timeout=1800)

        # Signal loader to stop and wait for it
        stop_event.set()
        loader_thread.join(timeout=120)

        total_records = extractor.total_loaded
        extractor.complete_pipeline_run("success", total_records)

        logger.info(f"{'='*60}")
        logger.info(f"Pipeline completed successfully")
        logger.info(f"Results by project:")
        total_tasks = 0
        total_with_req = 0
        total_req = 0
        for name, stats in sorted(project_stats.items()):
            logger.info(f"  {name}: {stats['tasks']:,} tasks, {stats['with_requirements']:,} with requirements, {stats['requirements']:,} requirements")
            total_tasks += stats["tasks"]
            total_with_req += stats["with_requirements"]
            total_req += stats["requirements"]

        logger.info(f"Totals:")
        logger.info(f"  Tasks processed: {total_tasks:,}")
        logger.info(f"  Tasks with requirements: {total_with_req:,}")
        logger.info(f"  Requirements loaded: {total_records:,}")
        logger.info(f"  Run ID: {extractor.run_id}")
        logger.info(f"{'='*60}")

        return str(extractor.run_id)

    except Exception as e:
        logger.error(f"{'='*60}")
        logger.error(f"Pipeline failed: {e}")
        logger.error(f"{'='*60}")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract Requirements from Swift API")
    parser.add_argument(
        "--min-project", type=int, default=13,
        help="Minimum project number (default: 13)"
    )
    parser.add_argument(
        "--projects", type=str, default=None,
        help="Specific project numbers to process, comma-separated (e.g., '17,18')"
    )
    parser.add_argument(
        "--workers", type=int, default=MAX_WORKERS,
        help=f"Number of parallel workers (default: {MAX_WORKERS})"
    )
    args = parser.parse_args()

    project_numbers = None
    if args.projects:
        project_numbers = [int(p.strip()) for p in args.projects.split(",")]

    run_requirements_pipeline(
        min_project_number=args.min_project,
        project_numbers=project_numbers,
        max_workers=args.workers
    )
