import json
import uuid
from typing import List, Dict, Any
from datetime import datetime, timezone
from config import SCHEMA_RAW, SCHEMA_PIPELINE, get_logger, get_db, retry_db

logger = get_logger("load")

class SupabaseLoader:
    def __init__(self):
        self.db = get_db()
        self.run_id: uuid.UUID = uuid.uuid4()

    def start_pipeline_run(self, pipeline_name: str) -> uuid.UUID:
        """Record pipeline run start"""
        self.db.execute(
            f'INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs (run_id, pipeline_name, status, started_at) '
            f'VALUES ($1, $2, $3, $4)',
            str(self.run_id), pipeline_name, "running", datetime.now(timezone.utc)
        )
        logger.info(f" Pipeline run started: {self.run_id}")
        return self.run_id

    def complete_pipeline_run(self, status: str, records_extracted: int = None, error_message: str = None):
        """Update pipeline run status"""
        self.db.execute(
            f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
            f'SET status = $1, completed_at = $2, records_extracted = $3, error_message = $4 '
            f'WHERE run_id = $5',
            status, datetime.now(timezone.utc), records_extracted, error_message, str(self.run_id)
        )
        logger.info(f" Pipeline run completed: {status}")

    def _clear_raw_table(self, table_name: str) -> None:
        """Delete all rows from a raw table before fresh load."""
        logger.info(f" Clearing {table_name}...")
        retry_db(
            lambda: self.db.execute(f'DELETE FROM {SCHEMA_RAW}.{table_name}'),
            description=f"clear {table_name}"
        )
        logger.info(f" Cleared {table_name}")

    def load_user_priorities_raw(self, records: List[Dict]) -> int:
        """Load user priorities as individual JSONB rows"""
        if not records:
            logger.info(f" No user priorities to load")
            return 0

        self._clear_raw_table("raw_user_priorities")

        run_id_str = str(self.run_id)
        tuples = [(run_id_str, record) for record in records]

        retry_db(
            lambda: self.db.copy_records(
                "raw_user_priorities",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "data"],
            ),
            description="copy raw_user_priorities"
        )

        logger.info(f" Total user priorities loaded: {len(records):,}")
        return len(records)

    def load_organizations_raw(self, orgs: List[Dict], user_id: str) -> int:
        """Load organizations as individual JSONB rows"""
        if not orgs:
            logger.info(f" No organizations to load")
            return 0

        self._clear_raw_table("raw_organizations")

        run_id_str = str(self.run_id)
        tuples = [(run_id_str, org) for org in orgs]

        retry_db(
            lambda: self.db.copy_records(
                "raw_organizations",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "data"],
            ),
            description="copy raw_organizations"
        )

        logger.info(f" Loaded {len(orgs)} organizations")
        return len(orgs)

    def load_projects_raw(self, projects: List[Dict]) -> int:
        """Load projects as individual JSONB rows"""
        if not projects:
            logger.info(f" No projects to load")
            return 0

        self._clear_raw_table("raw_projects")

        run_id_str = str(self.run_id)
        tuples = [(run_id_str, proj) for proj in projects]

        retry_db(
            lambda: self.db.copy_records(
                "raw_projects",
                schema_name=SCHEMA_RAW,
                records=tuples,
                columns=["run_id", "data"],
            ),
            description="copy raw_projects"
        )

        logger.info(f" Total projects loaded: {len(projects):,}")
        return len(projects)
