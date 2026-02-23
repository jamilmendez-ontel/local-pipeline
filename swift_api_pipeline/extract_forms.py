#!/usr/bin/env python3
"""
Extract Forms data from Swift API
Supports QA Forms for TS13+ projects
"""

import json
import requests
import time
import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Event
from datetime import datetime, timezone
from typing import List, Dict
from config import (
    SCHEMA_RAW, get_logger, retry_db, get_db, QA_FORMS
)
from base_extractor import BaseExtractor

logger = get_logger("forms")

PAGE_SIZE = 2000
MAX_RETRIES = 10
MAX_WORKERS = 6
LOAD_BATCH_SIZE = 10000


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


class FormsExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="forms_extract")

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
        csv_fieldnames = None  # Store headers from first page

        logger.info(f"[{display_name}] Starting extraction...")

        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if next_cursor:
                params["after"] = next_cursor

            for attempt in range(MAX_RETRIES):
                try:
                    resp = requests.get(url, headers=headers, params=params, timeout=60)

                    if resp.status_code == 204:
                        logger.info(f"[{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    resp.raise_for_status()

                    # Parse CSV response
                    # First page has headers, subsequent pages may not
                    if csv_fieldnames is None:
                        # First page - let DictReader detect headers
                        reader = csv.DictReader(io.StringIO(resp.text))
                        rows = list(reader)
                        csv_fieldnames = reader.fieldnames
                    else:
                        # Subsequent pages - use saved headers
                        # Check if first line looks like a header (matches our saved fieldnames)
                        lines = resp.text.strip().split('\n')
                        if lines and lines[0].startswith(csv_fieldnames[0]):
                            # Has header row - skip it
                            reader = csv.DictReader(io.StringIO(resp.text), fieldnames=None)
                            rows = list(reader)
                        else:
                            # No header row - use saved fieldnames
                            reader = csv.DictReader(io.StringIO(resp.text), fieldnames=csv_fieldnames)
                            rows = list(reader)

                    if not rows:
                        logger.info(f"[{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    # Stream batch to queue
                    result_queue.put((table_name, form_name, form_id, rows))
                    total_rows += len(rows)
                    page_count += 1

                    if page_count % 5 == 0:
                        logger.info(f"[{display_name}] Page {page_count} - {total_rows:,} rows")

                    # Check for next page
                    next_cursor = resp.headers.get("x-next")
                    if not next_cursor:
                        logger.info(f"[{display_name}] Complete - {total_rows:,} rows")
                        return total_rows

                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    logger.info(f"[{display_name}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        self.reauthenticate()
                        headers = {"Authorization": f"Bearer {self.token}", "Accept": "text/csv"}
            else:
                raise RuntimeError(f"[{display_name}] Failed after {MAX_RETRIES} attempts")

    def load_batch(self, table_name: str, batch: List[Dict]):
        """Load a batch of form responses to raw table"""
        run_id_str = str(self.run_id)
        tuples = [
            (run_id_str, record)
            for record in batch
        ]

        retry_db(
            lambda: self.db.copy_records(
                table_name,
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "data"],
            ),
            description=f"copy {table_name}"
        )
        self.increment_loaded(len(batch))

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
                logger.error(f"Loader error: {e}")
                result_queue.task_done()

        # Load all remaining data — must complete before transform runs
        remaining = sum(len(d) for d in pending_batches.values())
        logger.info(f"Flushing remaining data ({remaining:,} rows)...")
        for table_name, data in pending_batches.items():
            if data:
                try:
                    for i in range(0, len(data), LOAD_BATCH_SIZE):
                        batch = data[i:i + LOAD_BATCH_SIZE]
                        self.load_batch(table_name, batch)
                except Exception as e:
                    logger.error(f"Flush failed for {table_name} ({len(data):,} rows): {e}")
        logger.info("Loader complete")

    def clear_old_raw_data(self):
        """Clear old raw data (keep current run_id). Single query per table."""
        logger.info(f"Cleaning up old raw data (keeping run_id={self.run_id})...")
        for form_config in QA_FORMS.values():
            table = form_config["table_name"]
            retry_db(
                lambda t=table: self.db.execute(
                    f'DELETE FROM {SCHEMA_RAW}.{t} WHERE run_id != $1',
                    str(self.run_id)
                ),
                description=f"delete old {table}"
            )
        logger.info("Old raw data cleaned up")

    # start_pipeline_run() and complete_pipeline_run() inherited from BaseExtractor


def run_forms_pipeline(forms: Dict = None, max_workers: int = MAX_WORKERS):
    """Main pipeline for extracting forms data with parallel processing"""
    if forms is None:
        forms = QA_FORMS

    logger.info(f"\n{'='*60}")
    logger.info("Forms Extraction Pipeline (Parallel)")
    logger.info(f"Forms: {len(forms)}")
    logger.info(f"Workers: {max_workers}")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    extractor = FormsExtractor()

    try:
        extractor.start_pipeline_run()
        extractor.authenticate()

        logger.info(f"Processing {len(forms)} forms\n")

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
                    logger.error(f"[{form_config['display_name']}] FAILED: {e}")
                    form_rows[form_config["display_name"]] = 0

        # Wait for queue to be fully processed
        logger.info("Waiting for loader to finish...")
        _queue_join_with_timeout(result_queue, timeout=3600)

        # Signal loader to stop and wait for flush to complete.
        # No timeout — loader must finish flushing remaining partial batches
        # to raw before we proceed to transform.  A timeout here caused data
        # loss: rows accumulated in pending_batches (task_done() fires before
        # DB write) would be flushed by the daemon thread AFTER the transform
        # already ran, resulting in staging missing the last partial batch per
        # table (e.g. 60,000 instead of 63,942).
        stop_event.set()
        loader_thread.join()

        total_records = extractor.total_loaded

        # Clean up old raw data now that new extraction succeeded
        extractor.clear_old_raw_data()

        extractor.complete_pipeline_run("success", total_records)

        logger.info(f"\n{'='*60}")
        logger.info("Pipeline completed successfully")
        logger.info("\nRecords by form:")
        for name, count in sorted(form_rows.items()):
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
    parser = argparse.ArgumentParser(description="Extract Forms from Swift API")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"Number of parallel workers (default: {MAX_WORKERS})")
    args = parser.parse_args()

    run_forms_pipeline(max_workers=args.workers)
