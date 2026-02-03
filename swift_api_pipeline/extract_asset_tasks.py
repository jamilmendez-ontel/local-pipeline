#!/usr/bin/env python3
"""
Extract asset-tasks from Swift API for specified projects
Uses ThreadPoolExecutor for parallel extraction
"""

import sys
import uuid
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Lock
from datetime import datetime, timezone
from typing import List, Dict, Optional, Generator
from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, get_supabase_client

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 3  # Concurrent API threads
LOAD_BATCH_SIZE = 500


class AssetTaskExtractor:
    def __init__(self):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.token_lock = Lock()
        self.client = get_supabase_client()
        self.run_id = uuid.uuid4()
        self.total_loaded = 0
        self.load_lock = Lock()

    def authenticate(self) -> str:
        """Obtain authentication token (thread-safe)"""
        with self.token_lock:
            if self.token:
                return self.token

            url = f"{self.base_url}/api/auth/token"
            payload = {
                "grantType": "password",
                "include": ["profile", "firebaseToken"],
                "username": SWIFT_USERNAME,
                "password": SWIFT_PASSWORD,
                "scope": "openid"
            }

            response = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload
            )
            response.raise_for_status()
            self.token = response.json()["idToken"]
            print(f"[{datetime.now():%H:%M:%S}] Authenticated successfully")
            return self.token

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get project DIDs from reference table"""
        result = self.client.table("ref_ontel_techops_projects").select(
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

        print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Starting extraction...")

        while True:
            if after_ap and after_id:
                params['afterAp'] = after_ap
                params['after'] = after_id

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)

                    if resp.status_code == 204:
                        print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Complete - {project_rows:,} rows")
                        return project_rows

                    resp.raise_for_status()
                    data = resp.json().get("list", [])

                    if not data:
                        print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Complete - {project_rows:,} rows")
                        return project_rows

                    # Stream batch to queue immediately
                    result_queue.put((project_did, data))
                    project_rows += len(data)
                    page_count += 1

                    if page_count % 50 == 0:
                        print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Page {page_count} - {project_rows:,} rows")

                    # Handle keyset pagination
                    next_info = resp.json().get("next")
                    if not next_info:
                        print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Complete - {project_rows:,} rows")
                        return project_rows

                    after_ap = next_info.get("ap")
                    after_id = next_info.get("id")
                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    print(f"[{datetime.now():%H:%M:%S}] [{project_name}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        with self.token_lock:
                            self.token = None
                        self.authenticate()
                        headers = {"Authorization": f"Bearer {self.token}"}
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

        self.client.table("raw_asset_tasks").insert(rows).execute()

        with self.load_lock:
            self.total_loaded += len(batch)

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
                print(f"[{datetime.now():%H:%M:%S}] Loader error: {e}")

        # Load all remaining data
        print(f"[{datetime.now():%H:%M:%S}] Flushing remaining data...")
        for project_did, data in pending_batches.items():
            if data:
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(project_did, batch)
        print(f"[{datetime.now():%H:%M:%S}] Loader complete")

    def start_pipeline_run(self):
        """Record pipeline run start"""
        self.client.table("pipeline_runs").insert({
            "run_id": str(self.run_id),
            "pipeline_name": "asset_tasks_extract",
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        print(f"[{datetime.now():%H:%M:%S}] Pipeline run started: {self.run_id}")

    def complete_pipeline_run(self, status: str, records: int = None, error: str = None):
        """Update pipeline run status"""
        update_data = {
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }
        if records:
            update_data["records_extracted"] = records
        if error:
            update_data["error_message"] = error

        self.client.table("pipeline_runs").update(update_data).eq("run_id", str(self.run_id)).execute()
        print(f"[{datetime.now():%H:%M:%S}] Pipeline run completed: {status}")


def run_asset_task_pipeline(min_project_number: int = 13, max_workers: int = MAX_WORKERS):
    """Main pipeline for extracting asset-tasks with parallel processing"""
    print(f"\n{'='*60}")
    print(f"Asset-Task Extraction Pipeline (Parallel)")
    print(f"Projects: TECH-OPS TS{min_project_number}+")
    print(f"Workers: {max_workers}")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = AssetTaskExtractor()

    try:
        extractor.start_pipeline_run()
        extractor.authenticate()

        # Get projects from reference table
        projects = extractor.get_project_dids(min_project_number)
        print(f"[{datetime.now():%H:%M:%S}] Found {len(projects)} projects to process\n")

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
                    print(f"[{datetime.now():%H:%M:%S}] [{proj['project_name']}] FAILED: {e}")
                    project_rows[proj["project_name"]] = 0

        # Wait for queue to be fully processed first
        print(f"[{datetime.now():%H:%M:%S}] Waiting for loader to finish...")
        result_queue.join()

        # Signal loader to stop and wait for it
        stop_event.set()
        loader_thread.join(timeout=120)

        total_records = extractor.total_loaded
        extractor.complete_pipeline_run("success", total_records)

        print(f"\n{'='*60}")
        print(f"Pipeline completed successfully")
        print(f"\nRecords by project:")
        for name, count in sorted(project_rows.items()):
            print(f"  {name}: {count:,}")
        print(f"\nTotal loaded: {total_records:,}")
        print(f"Run ID: {extractor.run_id}")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Pipeline failed: {e}")
        print(f"{'='*60}\n")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract asset-tasks from Swift API")
    parser.add_argument("--min-project", type=int, default=13, help="Minimum project number (default: 13)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    args = parser.parse_args()

    run_asset_task_pipeline(min_project_number=args.min_project, max_workers=args.workers)
