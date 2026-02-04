#!/usr/bin/env python3
"""
Extract Timer Activities data from Swift API
Supports incremental loads with automatic date range calculation
"""

import sys
import uuid
import requests
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Lock, Event
from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
from typing import List, Dict, Optional, Tuple
from config import (
    SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, get_supabase_client,
    SCHEMA_RAW, SCHEMA_REFERENCE, SCHEMA_PIPELINE
)

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 3
LOAD_BATCH_SIZE = 500
TIMEZONE = "America/New_York"


def calculate_date_range() -> Tuple[str, str]:
    """
    Calculate the date range for extraction.

    Rules:
    - If today is NOT the 1st: start_date = 1st of current month, end_date = yesterday
    - If today IS the 1st: start_date = 1st of previous month, end_date = last day of previous month

    Returns:
        Tuple of (start_date, end_date) in YYYY-MM-DD format
    """
    today = datetime.now().date()

    if today.day == 1:
        # Today is the 1st - use previous month
        last_month = today - relativedelta(months=1)
        start_date = last_month.replace(day=1)
        end_date = today - timedelta(days=1)  # Last day of previous month
    else:
        # Normal case - 1st of current month to yesterday
        start_date = today.replace(day=1)
        end_date = today - timedelta(days=1)

    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


class TimerExtractor:
    def __init__(self):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.token_lock = Lock()
        self.client = get_supabase_client()
        self.run_id = uuid.uuid4()
        self.run_date = datetime.now().date()
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
        result = self.client.schema(SCHEMA_REFERENCE).table("ref_ontel_techops_projects").select(
            "project_did, project_name, project_number"
        ).gte("project_number", min_project_number).order("project_number").execute()

        return result.data

    def extract_project_timer(
        self,
        project: Dict,
        start_date: str,
        end_date: str,
        result_queue: Queue
    ) -> int:
        """Extract timer activities for a project within date range"""
        if not self.token:
            self.authenticate()

        project_did = project["project_did"]
        project_name = project["project_name"]
        project_number = project["project_number"]

        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/timer-activities/_report"

        # Convert dates to timestamps
        from_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        to_ts = int(datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp() * 1000)

        page = 0
        total_rows = 0

        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Starting extraction ({start_date} to {end_date})...")

        while True:
            params = {
                "tz": TIMEZONE,
                "dateFormat": "yyyy-MM-dd'T'HH:mm:ssZ",
                "filterOptions": json.dumps({
                    "dateRange": {
                        "useAfter": True,
                        "afterDate": from_ts,
                        "useBefore": True,
                        "beforeDate": to_ts
                    },
                    "project": project_did
                }),
                "pageSize": str(PAGE_SIZE),
                "page": str(page)
            }

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)

                    # Check for empty response
                    if resp.status_code == 204 or not resp.content.strip():
                        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Complete - {total_rows:,} rows")
                        return total_rows

                    resp.raise_for_status()

                    # Parse JSON response
                    try:
                        data = resp.json().get("list", [])
                    except ValueError:
                        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Complete - {total_rows:,} rows")
                        return total_rows

                    if not data:
                        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Complete - {total_rows:,} rows")
                        return total_rows

                    # Stream batch to queue with metadata
                    result_queue.put((project_did, project_number, start_date, end_date, data))
                    total_rows += len(data)
                    page += 1

                    if page % 5 == 0:
                        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Page {page} - {total_rows:,} rows")

                    # Check if last page
                    if len(data) < PAGE_SIZE:
                        print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Complete - {total_rows:,} rows")
                        return total_rows

                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    print(f"[{datetime.now():%H:%M:%S}] [TS{project_number}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        with self.token_lock:
                            self.token = None
                        self.authenticate()
                        headers = {"Authorization": f"Bearer {self.token}"}
            else:
                raise RuntimeError(f"[TS{project_number}] Failed after {MAX_RETRIES} attempts")

    def load_batch(self, project_did: str, start_date: str, end_date: str, batch: List[Dict]):
        """Load a batch of timer activities to raw table"""
        rows = [
            {
                "run_id": str(self.run_id),
                "run_date": str(self.run_date),
                "start_date": start_date,
                "end_date": end_date,
                "project_did": project_did,
                "data": record
            }
            for record in batch
        ]

        self.client.schema(SCHEMA_RAW).table("raw_timer_activities").insert(rows).execute()

        with self.load_lock:
            self.total_loaded += len(batch)

    def loader_worker(self, result_queue: Queue, stop_event: Event):
        """Background worker that loads batches from queue to database"""
        from queue import Empty
        pending_batches = {}  # (project_did, start_date, end_date) -> list of records

        while True:
            try:
                project_did, project_number, start_date, end_date, data = result_queue.get(timeout=0.5)
                key = (project_did, start_date, end_date)

                # Accumulate batches per project/date range
                if key not in pending_batches:
                    pending_batches[key] = []
                pending_batches[key].extend(data)

                # Load when batch is large enough
                while len(pending_batches[key]) >= LOAD_BATCH_SIZE:
                    batch = pending_batches[key][:LOAD_BATCH_SIZE]
                    pending_batches[key] = pending_batches[key][LOAD_BATCH_SIZE:]
                    self.load_batch(project_did, start_date, end_date, batch)

                result_queue.task_done()

            except Empty:
                if stop_event.is_set() and result_queue.empty():
                    break
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] Loader error: {e}")

        # Load all remaining data
        print(f"[{datetime.now():%H:%M:%S}] Flushing remaining data...")
        for key, data in pending_batches.items():
            if data:
                project_did, start_date, end_date = key
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(project_did, start_date, end_date, batch)
        print(f"[{datetime.now():%H:%M:%S}] Loader complete")

    def start_pipeline_run(self, start_date: str, end_date: str):
        """Record pipeline run start"""
        self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").insert({
            "run_id": str(self.run_id),
            "pipeline_name": "timer_extract",
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "start_date": start_date,
                "end_date": end_date,
                "run_date": str(self.run_date)
            }
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

        self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").update(update_data).eq("run_id", str(self.run_id)).execute()
        print(f"[{datetime.now():%H:%M:%S}] Pipeline run completed: {status}")


def run_timer_pipeline(
    start_date: str = None,
    end_date: str = None,
    min_project_number: int = 13,
    max_workers: int = MAX_WORKERS
):
    """Main pipeline for extracting timer activities with parallel processing"""

    # Calculate date range if not provided
    if start_date is None or end_date is None:
        start_date, end_date = calculate_date_range()

    print(f"\n{'='*60}")
    print(f"Timer Activities Extraction Pipeline")
    print(f"Date Range: {start_date} to {end_date}")
    print(f"Projects: TS{min_project_number}+")
    print(f"Workers: {max_workers}")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = TimerExtractor()

    try:
        extractor.start_pipeline_run(start_date, end_date)
        extractor.authenticate()

        # Get projects to extract
        projects = extractor.get_project_dids(min_project_number)
        print(f"[{datetime.now():%H:%M:%S}] Found {len(projects)} projects to extract\n")

        for p in projects:
            print(f"  - TS{p['project_number']}: {p['project_did']}")
        print()

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

        # Extract projects in parallel
        project_rows = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    extractor.extract_project_timer,
                    project,
                    start_date,
                    end_date,
                    result_queue
                ): project
                for project in projects
            }

            for future in as_completed(futures):
                project = futures[future]
                try:
                    rows = future.result()
                    project_rows[f"TS{project['project_number']}"] = rows
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] [TS{project['project_number']}] FAILED: {e}")
                    project_rows[f"TS{project['project_number']}"] = 0

        # Wait for queue to be fully processed
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
        print(f"Date Range: {start_date} to {end_date}")
        print(f"Run ID: {extractor.run_id}")
        print(f"{'='*60}\n")

        return str(extractor.run_id)

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Pipeline failed: {e}")
        print(f"{'='*60}\n")
        extractor.complete_pipeline_run("failed", error=str(e))
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract Timer Activities from Swift API")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD). Default: 1st of month")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD). Default: yesterday")
    parser.add_argument("--min-project", type=int, default=13, help="Minimum project number (default: 13)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    args = parser.parse_args()

    run_timer_pipeline(
        start_date=args.start_date,
        end_date=args.end_date,
        min_project_number=args.min_project,
        max_workers=args.workers
    )
