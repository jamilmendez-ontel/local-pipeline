"""
Shared base class for all Swift API extractors.

Consolidates duplicated code: authentication, pipeline tracking, and Supabase client setup.
Each extractor inherits from BaseExtractor and only implements extraction-specific logic.
"""

import uuid
import requests
from threading import Lock
from datetime import datetime, timezone
from typing import Optional

from config import (
    SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, create_supabase_client,
    SCHEMA_PIPELINE, get_logger, retry_supabase
)

logger = get_logger("base")


class BaseExtractor:
    """Base class providing authentication and pipeline tracking for all extractors."""

    def __init__(self, pipeline_name: str):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.token_lock = Lock()
        self.client = create_supabase_client()
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
        row = {
            "run_id": str(self.run_id),
            "pipeline_name": self._pipeline_name,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat()
        }
        if metadata:
            row["metadata"] = metadata

        retry_supabase(
            lambda: self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").insert(row).execute(),
            description="insert pipeline_runs"
        )
        logger.info(f"Pipeline run started: {self.run_id}")

    def complete_pipeline_run(self, status: str, records: int = None, error: str = None) -> None:
        """Update pipeline run status on completion."""
        update_data = {
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }
        if records is not None:
            update_data["records_extracted"] = records
        if error:
            update_data["error_message"] = error

        retry_supabase(
            lambda: self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").update(
                update_data
            ).eq("run_id", str(self.run_id)).execute(),
            description="update pipeline_runs"
        )
        logger.info(f"Pipeline run completed: {status}")

    def increment_loaded(self, count: int) -> None:
        """Thread-safe increment of total_loaded counter."""
        with self.load_lock:
            self.total_loaded += count
