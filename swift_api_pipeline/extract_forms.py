#!/usr/bin/env python3
"""
Extract Forms data from Swift API
Supports QA Forms for TS13+ projects
"""

import sys
import uuid
import requests
import time
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Lock, Event
from datetime import datetime, timezone
from typing import List, Dict, Optional
from config import (
    SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, get_supabase_client,
    SCHEMA_RAW, SCHEMA_PIPELINE
)

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

PAGE_SIZE = 2000
MAX_RETRIES = 10
MAX_WORKERS = 3
LOAD_BATCH_SIZE = 500

# QA Forms configuration (TS13+)
QA_FORMS = {
    "qa_ts13": {
        "form_id": "-NH1hUPkaKtPdd7BK9cb",
        "table_name": "raw_form_qa_ts13",
        "display_name": "QA Form TS13"
    },
    "qa_ts14": {
        "form_id": "-NXCg4vTDNVykN8ioMYp",
        "table_name": "raw_form_qa_ts14",
        "display_name": "QA Form TS14"
    },
    "qa_ts15": {
        "form_id": "-Np6o9OCL4RWIJq68HJe",
        "table_name": "raw_form_qa_ts15",
        "display_name": "QA Form TS15"
    },
    "qa_ts16": {
        "form_id": "-O9ACLN3je1w7oEoG5hY",
        "table_name": "raw_form_qa_ts16",
        "display_name": "QA Form TS16"
    },
    "qa_ts17": {
        "form_id": "-ONMD-cGBq-_3r9ybaAq",
        "table_name": "raw_form_qa_ts17",
        "display_name": "QA Form TS17"
    },
    "qa_ts18": {
        "form_id": "-O_J2hPlryTezP9RhujA",
        "table_name": "raw_form_qa_ts18",
        "display_name": "QA Form TS18"
    },
}


class FormsExtractor:
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

    def extract_form(
        self,
        form_name: str,
        form_config: Dict,
        result_queue: Queue
    ) -> int:
        """Extract all responses for a form, streaming batches to queue"""
        if not self.token:
            self.authenticate()

        form_id = form_config["form_id"]
        table_name = form_config["table_name"]
        display_name = form_config["display_name"]

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/csv"
        }
        url = f"{self.base_url}/api/forms/{form_id}/requirement-responses"

        next_cursor = None
        page_count = 0
        total_rows = 0

        print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Starting extraction...")

        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if next_cursor:
                params["after"] = next_cursor

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)

                    if resp.status_code == 204:
                        print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    resp.raise_for_status()

                    # Parse CSV response
                    reader = csv.DictReader(io.StringIO(resp.text))
                    rows = list(reader)

                    if not rows:
                        print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    # Stream batch to queue
                    result_queue.put((table_name, form_name, form_id, rows))
                    total_rows += len(rows)
                    page_count += 1

                    if page_count % 5 == 0:
                        print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Page {page_count} - {total_rows:,} rows")

                    # Check for next page
                    next_cursor = resp.headers.get("x-next")
                    if not next_cursor:
                        print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    print(f"[{datetime.now():%H:%M:%S}] [{display_name}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        with self.token_lock:
                            self.token = None
                        self.authenticate()
                        headers = {"Authorization": f"Bearer {self.token}", "Accept": "text/csv"}
            else:
                raise RuntimeError(f"[{display_name}] Failed after {MAX_RETRIES} attempts")

    def load_batch(self, table_name: str, batch: List[Dict]):
        """Load a batch of form responses to raw table"""
        rows = [
            {
                "run_id": str(self.run_id),
                "data": record
            }
            for record in batch
        ]

        self.client.schema(SCHEMA_RAW).table(table_name).insert(rows).execute()

        with self.load_lock:
            self.total_loaded += len(batch)

    def loader_worker(self, result_queue: Queue, stop_event: Event):
        """Background worker that loads batches from queue to database"""
        from queue import Empty
        pending_batches = {}  # table_name -> list of records

        while True:
            try:
                table_name, form_name, form_id, data = result_queue.get(timeout=0.5)

                # Accumulate batches per table
                if table_name not in pending_batches:
                    pending_batches[table_name] = []
                pending_batches[table_name].extend(data)

                # Load when batch is large enough
                while len(pending_batches[table_name]) >= LOAD_BATCH_SIZE:
                    batch = pending_batches[table_name][:LOAD_BATCH_SIZE]
                    pending_batches[table_name] = pending_batches[table_name][LOAD_BATCH_SIZE:]
                    self.load_batch(table_name, batch)

                result_queue.task_done()

            except Empty:
                if stop_event.is_set() and result_queue.empty():
                    break
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] Loader error: {e}")

        # Load all remaining data
        print(f"[{datetime.now():%H:%M:%S}] Flushing remaining data...")
        for table_name, data in pending_batches.items():
            if data:
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(table_name, batch)
        print(f"[{datetime.now():%H:%M:%S}] Loader complete")

    def start_pipeline_run(self):
        """Record pipeline run start"""
        self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").insert({
            "run_id": str(self.run_id),
            "pipeline_name": "forms_extract",
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

        self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").update(update_data).eq("run_id", str(self.run_id)).execute()
        print(f"[{datetime.now():%H:%M:%S}] Pipeline run completed: {status}")


def run_forms_pipeline(forms: Dict = None, max_workers: int = MAX_WORKERS):
    """Main pipeline for extracting forms data with parallel processing"""
    if forms is None:
        forms = QA_FORMS

    print(f"\n{'='*60}")
    print(f"Forms Extraction Pipeline (Parallel)")
    print(f"Forms: {len(forms)}")
    print(f"Workers: {max_workers}")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = FormsExtractor()

    try:
        extractor.start_pipeline_run()
        extractor.authenticate()

        print(f"[{datetime.now():%H:%M:%S}] Processing {len(forms)} forms\n")

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

        # Extract forms in parallel
        form_rows = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    extractor.extract_form,
                    form_name,
                    form_config,
                    result_queue
                ): (form_name, form_config)
                for form_name, form_config in forms.items()
            }

            for future in as_completed(futures):
                form_name, form_config = futures[future]
                try:
                    rows = future.result()
                    form_rows[form_config["display_name"]] = rows
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] [{form_config['display_name']}] FAILED: {e}")
                    form_rows[form_config["display_name"]] = 0

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
        print(f"\nRecords by form:")
        for name, count in sorted(form_rows.items()):
            print(f"  {name}: {count:,}")
        print(f"\nTotal loaded: {total_records:,}")
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
    parser = argparse.ArgumentParser(description="Extract Forms from Swift API")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    args = parser.parse_args()

    run_forms_pipeline(max_workers=args.workers)
