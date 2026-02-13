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

    def complete_pipeline_run(self, status: str, records: int = None, error: str = None) -> None:
        """Update pipeline run status on completion."""
        retry_db(
            lambda: self.db.execute(
                f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
                f'SET status = $1, completed_at = $2, records_extracted = $3, error_message = $4 '
                f'WHERE run_id = $5',
                status, datetime.now(timezone.utc), records, error, str(self.run_id)
            ),
            description="update pipeline_runs"
        )
        logger.info(f"Pipeline run completed: {status}")

    def increment_loaded(self, count: int) -> None:
        """Thread-safe increment of total_loaded counter."""
        with self.load_lock:
            self.total_loaded += count
