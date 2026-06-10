"""Extract Swift Invoicing Form requirement-responses into
data_raw.raw_invoicing_form (one table, form_did column).

Run standalone:  python extract_invoicing_form.py
"""
import csv
import io
import logging
import time

import requests

from base_extractor import BaseExtractor
from config import (
    INVOICING_FORMS, INVOICING_RAW_TABLE, PAGE_SIZE, SCHEMA_RAW,
)
from db import retry_db

logger = logging.getLogger(__name__)

LOAD_BATCH_SIZE = 10000

# Transient-error retry for the Swift API. A brief 5xx / 429 / connection blip
# must not abort the whole nightly load: on 2026-06-10 a single 503 on one form
# (-OmoXCo93LkiEzTrsVDy, TS19) killed the run. Retry the page with exponential
# backoff (2, 4, 8, 16, 32s) before giving up.
MAX_PAGE_RETRIES = 5
RETRY_BACKOFF_BASE = 2  # seconds


class InvoicingFormExtractor(BaseExtractor):
    def __init__(self):
        super().__init__(pipeline_name="invoicing_extract")

    def clear_old_raw_data(self):
        retry_db(
            lambda: self.db.execute(
                f"DELETE FROM {SCHEMA_RAW}.{INVOICING_RAW_TABLE} WHERE run_id != $1",
                str(self.run_id),
            ),
            description=f"delete old {INVOICING_RAW_TABLE}",
        )

    def _load_batch(self, batch):
        # batch: list of (form_did, row_dict)
        tuples = [(str(self.run_id), form_did, row) for (form_did, row) in batch]
        retry_db(
            lambda: self.db.copy_records(
                INVOICING_RAW_TABLE,
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "form_did", "data"],
            ),
            description=f"copy {INVOICING_RAW_TABLE}",
        )
        self.increment_loaded(len(batch))

    def _fetch_page(self, url, headers, params):
        """GET one page of requirement-responses, surviving transient Swift
        failures. Re-authenticates on 401, and retries 5xx / 429 / connection
        errors with exponential backoff. Raises once retries are exhausted.
        Returns a response the caller still checks for 204 / other 4xx."""
        for attempt in range(1, MAX_PAGE_RETRIES + 1):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=60)
            except requests.exceptions.RequestException as e:
                if attempt == MAX_PAGE_RETRIES:
                    raise
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "[invoicing] request error: %s (attempt %d/%d, retrying in %ds)",
                    e, attempt, MAX_PAGE_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                # Re-auth and retry immediately; does not consume the backoff budget meaningfully.
                headers["Authorization"] = f"Bearer {self.reauthenticate()}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_PAGE_RETRIES:
                    resp.raise_for_status()  # exhausted: surface the real error
                wait = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "[invoicing] HTTP %s from Swift (attempt %d/%d, retrying in %ds)",
                    resp.status_code, attempt, MAX_PAGE_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            return resp
        return resp  # only reached if every attempt was a 401; caller raises on it

    def extract_form(self, form_did, batch):
        headers = {"Authorization": f"Bearer {self.authenticate()}", "Accept": "text/csv"}
        url = f"{self.base_url}/api/forms/{form_did}/requirement-responses"
        next_cursor = None
        fieldnames = None
        form_rows = 0
        page_count = 0
        logger.info("[invoicing %s] starting extraction...", form_did)
        while True:
            params = {"pageSize": str(PAGE_SIZE)}
            if next_cursor:
                params["after"] = next_cursor
            resp = self._fetch_page(url, headers, params)
            if resp.status_code == 204:
                break
            resp.raise_for_status()
            if fieldnames is None:
                reader = csv.DictReader(io.StringIO(resp.text))
                rows = list(reader)
                fieldnames = reader.fieldnames
            else:
                first = resp.text.split("\n", 1)[0]
                use_names = None if (fieldnames and first.startswith(fieldnames[0])) else fieldnames
                rows = list(csv.DictReader(io.StringIO(resp.text), fieldnames=use_names))
            for r in rows:
                batch.append((form_did, r))
                form_rows += 1
                if len(batch) >= LOAD_BATCH_SIZE:
                    self._load_batch(batch)
                    batch.clear()
            page_count += 1
            logger.info(
                "[invoicing %s] page %d: %s rows so far", form_did, page_count, f"{form_rows:,}"
            )
            next_cursor = resp.headers.get("x-next")
            if not next_cursor:
                break
        logger.info("[invoicing %s] done: %s rows in %d pages", form_did, f"{form_rows:,}", page_count)
        return form_rows


def run_invoicing_extract():
    """Extract all configured invoicing forms. Returns the run_id (str)."""
    ex = InvoicingFormExtractor()
    ex.start_pipeline_run(metadata={"forms": INVOICING_FORMS})
    total = 0
    try:
        ex.clear_old_raw_data()
        batch = []
        for form_did in INVOICING_FORMS:
            total += ex.extract_form(form_did, batch)
        if batch:
            ex._load_batch(batch)
            batch.clear()
        ex.complete_pipeline_run("success", records=total)
        logger.info("Invoicing extract complete: %s rows", f"{total:,}")
    except Exception as e:  # noqa: BLE001
        ex.complete_pipeline_run("failed", records=total, error=str(e))
        raise
    return str(ex.run_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_invoicing_extract()
