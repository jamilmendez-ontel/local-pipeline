#!/usr/bin/env python3
"""
Swift API to Supabase Raw JSONB Pipeline
Full refresh extraction with raw data preservation
"""

import sys
from datetime import datetime
from extract import SwiftAPIExtractor
from load import SupabaseLoader

def run_pipeline():
    """Main pipeline orchestration"""
    print(f"\n{'='*60}")
    print(f"Swift API -> Supabase Raw JSONB Pipeline")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = SwiftAPIExtractor()
    loader = SupabaseLoader()

    try:
        # Start pipeline run tracking
        loader.start_pipeline_run("swift_api_full_refresh")

        # Step 1: Extract user priorities
        print(f"\n[STEP 1] Extracting user priorities...")
        user_priorities = extractor.extract_user_priorities()

        # Step 2: Extract organizations
        print(f"\n[STEP 2] Extracting organizations...")
        organizations = extractor.extract_organizations()

        # Step 3: Extract all projects
        print(f"\n[STEP 3] Extracting projects...")
        projects = extractor.extract_all_projects()

        total_records = len(user_priorities) + len(organizations) + len(projects)
        print(f"\n{'='*60}")
        print(f"Extraction Summary:")
        print(f"  User Priorities: {len(user_priorities):,}")
        print(f"  Organizations: {len(organizations):,}")
        print(f"  Projects: {len(projects):,}")
        print(f"  Total Records: {total_records:,}")
        print(f"{'='*60}\n")

        # Step 4: Load to Supabase
        print(f"\n[STEP 4] Loading to Supabase...")

        loader.load_user_priorities_raw(user_priorities)
        loader.load_organizations_raw(organizations, extractor.user_id)
        loader.load_projects_raw(projects)

        # Mark pipeline as successful
        loader.complete_pipeline_run("success", total_records)

        print(f"\n{'='*60}")
        print(f"Pipeline completed successfully")
        print(f"Run ID: {loader.run_id}")
        print(f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'='*60}\n")

        return 0

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Pipeline failed: {str(e)}")
        print(f"{'='*60}\n")

        loader.complete_pipeline_run("failed", error_message=str(e))
        return 1

if __name__ == "__main__":
    sys.exit(run_pipeline())
