import uuid
from typing import List, Dict, Any
from datetime import datetime
from supabase import Client
from config import get_supabase_client, SCHEMA_RAW, SCHEMA_PIPELINE

class SupabaseLoader:
    def __init__(self):
        self.client: Client = get_supabase_client()
        self.run_id: uuid.UUID = uuid.uuid4()

    def start_pipeline_run(self, pipeline_name: str) -> uuid.UUID:
        """Record pipeline run start"""
        result = self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").insert({
            "run_id": str(self.run_id),
            "pipeline_name": pipeline_name,
            "status": "running",
            "started_at": datetime.utcnow().isoformat()
        }).execute()

        print(f"[{datetime.now():%H:%M:%S}] Pipeline run started: {self.run_id}")
        return self.run_id

    def complete_pipeline_run(self, status: str, records_extracted: int = None, error_message: str = None):
        """Update pipeline run status"""
        update_data = {
            "status": status,
            "completed_at": datetime.utcnow().isoformat()
        }

        if records_extracted is not None:
            update_data["records_extracted"] = records_extracted

        if error_message:
            update_data["error_message"] = error_message

        self.client.schema(SCHEMA_PIPELINE).table("pipeline_runs").update(update_data).eq("run_id", str(self.run_id)).execute()

        print(f"[{datetime.now():%H:%M:%S}] Pipeline run completed: {status}")

    def load_user_priorities_raw(self, records: List[Dict]) -> int:
        """Load user priorities as individual JSONB rows"""
        if not records:
            print(f"[{datetime.now():%H:%M:%S}] No user priorities to load")
            return 0

        # Insert records in batches for efficiency
        batch_size = 500
        total_loaded = 0

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]

            # Create individual row for each record
            rows = [
                {"run_id": str(self.run_id), "data": record}
                for record in batch
            ]

            self.client.schema(SCHEMA_RAW).table("raw_user_priorities").insert(rows).execute()
            total_loaded += len(batch)

            print(f"[{datetime.now():%H:%M:%S}] Loaded {total_loaded:,} / {len(records):,} user priorities")

        print(f"[{datetime.now():%H:%M:%S}] Total user priorities loaded: {total_loaded:,}")
        return total_loaded

    def load_organizations_raw(self, orgs: List[Dict], user_id: str) -> int:
        """Load organizations as individual JSONB rows"""
        if not orgs:
            print(f"[{datetime.now():%H:%M:%S}] No organizations to load")
            return 0

        # Create individual row for each organization
        rows = [
            {"run_id": str(self.run_id), "data": org}
            for org in orgs
        ]

        self.client.schema(SCHEMA_RAW).table("raw_organizations").insert(rows).execute()

        print(f"[{datetime.now():%H:%M:%S}] Loaded {len(orgs)} organizations")
        return len(orgs)

    def load_projects_raw(self, projects: List[Dict]) -> int:
        """Load projects as individual JSONB rows"""
        if not projects:
            print(f"[{datetime.now():%H:%M:%S}] No projects to load")
            return 0

        # Insert in batches for efficiency
        batch_size = 500
        total_loaded = 0

        for i in range(0, len(projects), batch_size):
            batch = projects[i:i + batch_size]

            rows = [
                {"run_id": str(self.run_id), "data": proj}
                for proj in batch
            ]

            self.client.schema(SCHEMA_RAW).table("raw_projects").insert(rows).execute()
            total_loaded += len(batch)

            print(f"[{datetime.now():%H:%M:%S}] Loaded {total_loaded:,} / {len(projects):,} projects")

        print(f"[{datetime.now():%H:%M:%S}] Total projects loaded: {total_loaded:,}")
        return total_loaded
