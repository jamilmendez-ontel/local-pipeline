#!/usr/bin/env python3
"""
Swift API to Supabase Raw JSONB Pipeline
Full refresh extraction with raw data preservation
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from config import SCHEMA_PIPELINE, retry_db
from extract import SwiftAPIExtractor
from load import SupabaseLoader


def _snapshot_hash(records):
    """Order-independent content hash of a fetched snapshot."""
    canonical = sorted(json.dumps(r, sort_keys=True, default=str) for r in records)
    return hashlib.md5("\n".join(canonical).encode("utf-8")).hexdigest()


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


def run_user_priorities_extract(skip_if_unchanged=False):
    """Extract user priorities only. Returns run_id string.

    skip_if_unchanged=True changes the contract: returns a
    (run_id, snapshot_hash) tuple on load, or None when the fetched snapshot
    hashes identically to the last loaded one and the whole delete+reload was
    skipped. The caller must then skip the transform too, and only advance
    the pipeline.content_watermarks row AFTER a successful transform (so a
    failed transform can never leave staging stale behind a current
    watermark). The full reload every 5 minutes regardless of change was a
    main driver of the Supabase Disk IO budget depletion (2026-07-09).
    Escape hatches: USER_PRIORITIES_FORCE=1 env, or delete the watermark row.
    """
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

        snapshot_hash = None
        if skip_if_unchanged:
            snapshot_hash = _snapshot_hash(user_priorities)
            if os.environ.get("USER_PRIORITIES_FORCE") == "1":
                print("USER_PRIORITIES_FORCE=1: loading regardless of watermark")
            else:
                prev_hash = retry_db(
                    lambda: loader.db.fetchval(
                        f"SELECT content_hash FROM {SCHEMA_PIPELINE}.content_watermarks "
                        f"WHERE pipeline_name = $1",
                        "user_priorities",
                    ),
                    description="read user_priorities content watermark",
                )
                if prev_hash == snapshot_hash:
                    print(f"Snapshot unchanged since last load (hash {snapshot_hash[:12]}): "
                          f"skipping load")
                    loader.complete_pipeline_run("success", len(user_priorities))
                    return None

        print(f"\n[STEP 2] Loading to Supabase...")
        loader.load_user_priorities_raw(user_priorities)

        loader.complete_pipeline_run("success", len(user_priorities))

        print(f"\n{'='*60}")
        print(f"Pipeline completed successfully")
        print(f"Run ID: {loader.run_id}")
        print(f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"{'='*60}\n")

        if skip_if_unchanged:
            return (str(loader.run_id), snapshot_hash)
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
