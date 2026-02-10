#!/usr/bin/env python3
"""
Swift API to Supabase Raw JSONB Pipeline
Full refresh extraction with raw data preservation
"""

import sys
from datetime import datetime
from extract import SwiftAPIExtractor
from load import SupabaseLoader


def run_orgs_projects_extract():
    """Extract organizations and projects only. Returns run_id string."""
    print(f"\n{'='*60}")
    print(f"Organizations & Projects Extraction")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = SwiftAPIExtractor()
    loader = SupabaseLoader()

    try:
        loader.start_pipeline_run("orgs_projects_extract")

        print(f"\n[STEP 1] Extracting organizations...")
        organizations = extractor.extract_organizations()

        print(f"\n[STEP 2] Extracting projects...")
        projects = extractor.extract_all_projects()

        total_records = len(organizations) + len(projects)
        print(f"\n{'='*60}")
        print(f"Extraction Summary:")
        print(f"  Organizations: {len(organizations):,}")
        print(f"  Projects: {len(projects):,}")
        print(f"  Total Records: {total_records:,}")
        print(f"{'='*60}\n")

        print(f"\n[STEP 3] Loading to Supabase...")
        loader.load_organizations_raw(organizations, extractor.user_id)
        loader.load_projects_raw(projects)

        loader.complete_pipeline_run("success", total_records)

        print(f"\n{'='*60}")
        print(f"Pipeline completed successfully")
        print(f"Run ID: {loader.run_id}")
        print(f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'='*60}\n")

        return str(loader.run_id)

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Pipeline failed: {str(e)}")
        print(f"{'='*60}\n")
        loader.complete_pipeline_run("failed", error_message=str(e))
        raise


def run_user_priorities_extract():
    """Extract user priorities only. Returns run_id string."""
    print(f"\n{'='*60}")
    print(f"User Priorities Extraction")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    extractor = SwiftAPIExtractor()
    loader = SupabaseLoader()

    try:
        loader.start_pipeline_run("user_priorities_extract")

        print(f"\n[STEP 1] Extracting user priorities...")
        user_priorities = extractor.extract_user_priorities()

        print(f"\n{'='*60}")
        print(f"Extraction Summary:")
        print(f"  User Priorities: {len(user_priorities):,}")
        print(f"{'='*60}\n")

        print(f"\n[STEP 2] Loading to Supabase...")
        loader.load_user_priorities_raw(user_priorities)

        loader.complete_pipeline_run("success", len(user_priorities))

        print(f"\n{'='*60}")
        print(f"Pipeline completed successfully")
        print(f"Run ID: {loader.run_id}")
        print(f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'='*60}\n")

        return str(loader.run_id)

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Pipeline failed: {str(e)}")
        print(f"{'='*60}\n")
        loader.complete_pipeline_run("failed", error_message=str(e))
        raise


# Keep backward compatibility for direct execution
def run_pipeline():
    """Run both orgs/projects and user priorities (legacy entry point)"""
    run_orgs_projects_extract()
    run_user_priorities_extract()
    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline())
