"""
Shared base class for all Swift API extractors.

Consolidates duplicated code: authentication, pipeline tracking, and database setup.
Each extractor inherits from BaseExtractor and only implements extraction-specific logic.
"""

import json
import uuid
import requests
from threading import Lock
from datetime import datetime, timezone
from typing import Optional

from config import (
    SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD,
    SCHEMA_PIPELINE, get_logger, get_db, retry_db
)

logger = get_logger("base")


class BaseExtractor:
    """Base class providing authentication and pipeline tracking for all extractors."""

    def __init__(self, pipeline_name: str):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.token_lock = Lock()
        self.db = get_db()
        self.run_id = uuid.uuid4()
        self.total_loaded = 0
        self.load_lock = Lock()
        self._pipeline_name = pipeline_name

    def authenticate(self) -> str:
        """Obtain authentication token (thread-safe with double-check locking)."""
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
            logger.info("Authenticated successfully")
            return self.token

    def reauthenticate(self) -> str:
        """Force re-authentication (e.g., after a 401)."""
        with self.token_lock:
            self.token = None
        return self.authenticate()

    def get_auth_headers(self) -> dict:
        """Get Authorization headers, ensuring token is valid."""
        if not self.token:
            self.authenticate()
        return {"Authorization": f"Bearer {self.token}"}

    def start_pipeline_run(self, metadata: dict = None) -> None:
        """Record pipeline run start in the pipeline schema."""
        retry_db(
            lambda: self.db.execute(
                f'INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs (run_id, pipeline_name, status, started_at, metadata) '
                f'VALUES ($1, $2, $3, $4, $5)',
                str(self.run_id), self._pipeline_name, "running",
                datetime.now(timezone.utc), metadata
            ),
            description="insert pipeline_runs"
        )
        logger.info(f"Pipeline run started: {self.run_id}")

    def complete_pipeline_run(self, status: str, records: int = None,
                              error: str = None, project_counts: dict = None) -> None:
        """Update pipeline run status on completion.

        When project_counts is supplied, merge {"project_counts": {...}} into the
        run's metadata jsonb so the next run can use it as a row-count baseline.
        """
        if project_counts is not None:
            payload = json.dumps({"project_counts": project_counts})
            retry_db(
                lambda: self.db.execute(
                    f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
                    f'SET status = $1, completed_at = $2, records_extracted = $3, '
                    f'error_message = $4, '
                    f'metadata = COALESCE(metadata, \'{{}}\'::jsonb) || $5::jsonb '
                    f'WHERE run_id = $6',
                    status, datetime.now(timezone.utc), records, error, payload,
                    str(self.run_id)
                ),
                description="update pipeline_runs (with counts)"
            )
        else:
            retry_db(
                lambda: self.db.execute(
                    f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
                    f'SET status = $1, completed_at = $2, records_extracted = $3, '
                    f'error_message = $4 WHERE run_id = $5',
                    status, datetime.now(timezone.utc), records, error, str(self.run_id)
                ),
                description="update pipeline_runs"
            )
        logger.info(f"Pipeline run completed: {status}")

    def get_previous_project_counts(self) -> tuple[dict, int]:
        """Return (project_counts dict, total_records) of the most recent prior
        successful run for this pipeline, or ({}, 0) if there is none."""
        row = retry_db(
            lambda: self.db.fetchrow(
                f"SELECT records_extracted, metadata->'project_counts' AS project_counts "
                f"FROM {SCHEMA_PIPELINE}.pipeline_runs "
                f"WHERE pipeline_name = $1 AND status = 'success' "
                f"AND completed_at IS NOT NULL AND run_id <> $2 "
                f"ORDER BY completed_at DESC LIMIT 1",
                self._pipeline_name, str(self.run_id)
            ),
            description="fetch previous project counts"
        )
        if not row:
            return {}, 0
        raw = row["project_counts"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        counts = {k: int(v) for k, v in (raw or {}).items()}
        return counts, int(row["records_extracted"] or 0)

    def increment_loaded(self, count: int) -> None:
        """Thread-safe increment of total_loaded counter."""
        with self.load_lock:
            self.total_loaded += count
