#!/usr/bin/env python3
"""
Extract Timer Activities data from Swift API
Supports incremental loads with automatic date range calculation
"""

import json
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Event
from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
from typing import List, Dict, Tuple
from config import (
    SCHEMA_RAW, SCHEMA_REFERENCE, get_logger, retry_db, get_db
)
from base_extractor import BaseExtractor

logger = get_logger("timer")

PAGE_SIZE = 1000
MAX_RETRIES = 10
MAX_WORKERS = 6
LOAD_BATCH_SIZE = 1000
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
    import zoneinfo
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()

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


class TimerExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="timer_extract")
        import zoneinfo
        self.run_date = datetime.now(zoneinfo.ZoneInfo(TIMEZONE)).date()

    def get_project_dids(self, min_project_number: int = 13) -> List[Dict]:
        """Get project DIDs from reference table"""
        rows = self.db.fetch(
            f'SELECT project_did, project_name, project_number '
            f'FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects '
            f'WHERE project_number >= $1 '
            f'ORDER BY project_number',
            min_project_number
        )
        return [dict(r) for r in rows]

    def _split_into_daily_chunks(self, start_date: str, end_date: str) -> List[Tuple[str, str]]:
        """Split a date range into daily chunks to avoid API silent truncation.

        The Swift API silently drops older rows when a single date range
        returns more than ~1,000 rows total (regardless of pagination).
        Daily chunks keep each request well under that limit.
        """
        from datetime import date as _date
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        chunks = []
        d = start
        while d <= end:
            chunks.append((d.isoformat(), d.isoformat()))
            d += timedelta(days=1)
        return chunks

    def extract_project_timer(
        self,
        project: Dict,
        start_date: str,
        end_date: str,
        result_queue: Queue
    ) -> int:
        """Extract timer activities for a project within date range.

        Splits into weekly chunks to work around the Swift API's ~3K row
        silent truncation limit per request.
        """
        if not self.token:
            self.authenticate()

        project_did = project["project_did"]
        project_name = project["project_name"]
        project_number = project["project_number"]

        chunks = self._split_into_daily_chunks(start_date, end_date)
        logger.info(f"[TS{project_number}] Starting extraction ({start_date} to {end_date}, {len(chunks)} daily chunks)...")

        total_rows = 0

        for chunk_start, chunk_end in chunks:
            chunk_rows = self._extract_chunk(
                project_did, project_number, chunk_start, chunk_end,
                start_date, end_date, result_queue
            )
            total_rows += chunk_rows

        logger.info(f"[TS{project_number}] Complete - {total_rows:,} rows")
        return total_rows

    def _extract_chunk(
        self,
        project_did: str,
        project_number: int,
        chunk_start: str,
        chunk_end: str,
        overall_start: str,
        overall_end: str,
        result_queue: Queue
    ) -> int:
        """Extract a single weekly chunk for one project.

        API requests use chunk_start/chunk_end, but raw records are tagged
        with the overall month start/end so downstream behaviour is unchanged.
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.base_url}/api/timer-activities/_report"

        # Convert chunk dates to timestamps for the API call (timezone-aware)
        import zoneinfo
        tz = zoneinfo.ZoneInfo(TIMEZONE)
        from_dt = datetime.strptime(chunk_start, "%Y-%m-%d").replace(tzinfo=tz)
        to_dt = datetime.strptime(chunk_end + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        from_ts = int(from_dt.timestamp() * 1000)
        to_ts = int(to_dt.timestamp() * 1000)

        page = 0
        chunk_rows = 0

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
                        return chunk_rows

                    resp.raise_for_status()

                    # Parse JSON response
                    try:
                        data = resp.json().get("list", [])
                    except ValueError:
                        return chunk_rows

                    if not data:
                        return chunk_rows

                    # Stream batch to queue with metadata
                    # Tag with overall month range, not chunk range
                    result_queue.put((project_did, project_number, overall_start, overall_end, data))
                    chunk_rows += len(data)
                    page += 1

                    if page % 5 == 0:
                        logger.info(f"[TS{project_number}] {start_date}..{end_date} page {page} - {chunk_rows:,} rows")

                    # Check if last page
                    if len(data) < PAGE_SIZE:
                        return chunk_rows

                    break

                except requests.RequestException as e:
                    wait_time = min(0.5 * (2 ** attempt), 30)
                    logger.error(f"[TS{project_number}] Retry {attempt + 1}/{MAX_RETRIES}: {e}")
                    time.sleep(wait_time)

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        self.reauthenticate()
                        headers = {"Authorization": f"Bearer {self.token}"}
            else:
                raise RuntimeError(f"[TS{project_number}] Failed after {MAX_RETRIES} attempts")

    def load_batch(self, project_did: str, start_date: str, end_date: str, batch: List[Dict]):
        """Load a batch of timer activities to raw table"""
        from datetime import date as _date
        run_id_str = str(self.run_id)
        # COPY binary protocol requires datetime.date objects for date columns
        run_date_val = self.run_date if isinstance(self.run_date, _date) else _date.fromisoformat(str(self.run_date))
        start_date_val = _date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
        end_date_val = _date.fromisoformat(end_date) if isinstance(end_date, str) else end_date

        tuples = [
            (run_id_str, run_date_val, start_date_val, end_date_val, project_did, record)
            for record in batch
        ]

        retry_db(
            lambda: self.db.copy_records(
                "raw_timer_activities",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "run_date", "start_date", "end_date", "project_did", "data"],
            ),
            description="copy raw_timer_activities"
        )
        self.increment_loaded(len(batch))

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
                logger.error(f"Loader error: {e}")
                result_queue.task_done()

        # Load all remaining data
        logger.info("Flushing remaining data...")
        for key, data in pending_batches.items():
            if data:
                project_did, start_date, end_date = key
                for i in range(0, len(data), LOAD_BATCH_SIZE):
                    batch = data[i:i + LOAD_BATCH_SIZE]
                    self.load_batch(project_did, start_date, end_date, batch)
        logger.info("Loader complete")

    # start_pipeline_run() and complete_pipeline_run() inherited from BaseExtractor


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

    logger.info(f"\n{'='*60}")
    logger.info(f"Timer Activities Extraction Pipeline")
    logger.info(f"Date Range: {start_date} to {end_date}")
    logger.info(f"Projects: TS{min_project_number}+")
    logger.info(f"Workers: {max_workers}")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*60}\n")

    extractor = TimerExtractor()

    try:
        extractor.start_pipeline_run(metadata={
            "start_date": start_date,
            "end_date": end_date,
            "run_date": str(extractor.run_date)
        })
        extractor.authenticate()

        # Get projects to extract
        projects = extractor.get_project_dids(min_project_number)
        logger.info(f"Found {len(projects)} projects to extract\n")

        for p in projects:
            logger.info(f"  - TS{p['project_number']}: {p['project_did']}")
        logger.info("")

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
                    logger.error(f"[TS{project['project_number']}] FAILED: {e}")
                    project_rows[f"TS{project['project_number']}"] = 0

        # Wait for queue to be fully processed
        logger.info("Waiting for loader to finish...")
        _queue_join_with_timeout(result_queue, timeout=600)

        # Signal loader to stop and wait for it
        stop_event.set()
        loader_thread.join(timeout=120)

        total_records = extractor.total_loaded
        extractor.complete_pipeline_run("success", total_records)

        logger.info(f"\n{'='*60}")
        logger.info(f"Pipeline completed successfully")
        logger.info(f"\nRecords by project:")
        for name, count in sorted(project_rows.items()):
            logger.info(f"  {name}: {count:,}")
        logger.info(f"\nTotal loaded: {total_records:,}")
        logger.info(f"Date Range: {start_date} to {end_date}")
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
