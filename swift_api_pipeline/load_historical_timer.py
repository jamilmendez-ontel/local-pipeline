#!/usr/bin/env python3
"""
Load historical timer activities from Excel into Supabase.

Reads timer_activities_data.xlsx (266k+ rows), loads into
data_raw.raw_timer_activities_historical, then transforms into
data_staging.stg_timer_activities.

Usage:
    python load_historical_timer.py
"""

import os
import sys
import uuid
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import asyncpg
from dotenv import load_dotenv

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    get_supabase_client, create_supabase_client,
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_REFERENCE,
    retry_supabase,
)
from transform import transform_historical_timer_activities

load_dotenv()

# Constants
EXCEL_PATH = Path(__file__).parent.parent / "scripts-reference" / "data_sample" / "timer_activities_data.xlsx"
SOURCE_FILE = "timer_activities_data.xlsx"
TZ_ET = ZoneInfo("America/New_York")
BATCH_SIZE = 1000

# Local Supabase DB connection for migration
DB_HOST = "127.0.0.1"
DB_PORT = 54322
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASS = "postgres"


def normalize_date(val) -> str:
    """Convert mixed int/string dates like 20230101 or '20230101' to '2023-01-01'."""
    if pd.isna(val):
        return None
    s = str(int(val))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def localize_timestamp(ts) -> str:
    """Convert pandas Timestamp to America/New_York ISO 8601 string."""
    if pd.isna(ts):
        return None
    if isinstance(ts, pd.Timestamp):
        if ts.tzinfo is None:
            ts = ts.tz_localize(TZ_ET)
        return ts.isoformat()
    return str(ts)


async def run_migration():
    """Create the raw_timer_activities_historical table via asyncpg."""
    migration_sql = Path(__file__).parent / "migrations" / "015_raw_timer_historical.sql"
    sql = migration_sql.read_text()

    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS
    )
    try:
        await conn.execute(sql)
        print(f"[{datetime.now():%H:%M:%S}] Migration 015 applied successfully")
    finally:
        await conn.close()


def read_excel() -> pd.DataFrame:
    """Read and validate the Excel file."""
    print(f"[{datetime.now():%H:%M:%S}] Reading Excel file: {EXCEL_PATH}")
    df = pd.read_excel(EXCEL_PATH)
    print(f"[{datetime.now():%H:%M:%S}] Read {len(df):,} rows, {len(df.columns)} columns")
    print(f"  Columns: {list(df.columns)}")
    print(f"  Projects: {sorted(df['Project'].unique())}")
    return df


def prepare_raw_records(df: pd.DataFrame, run_id: str) -> list:
    """Convert DataFrame rows into records for raw_timer_activities_historical."""
    print(f"[{datetime.now():%H:%M:%S}] Preparing raw records...")
    records = []

    for _, row in df.iterrows():
        start_date = normalize_date(row["start_date"])
        end_date = normalize_date(row["end_date"])
        run_date = normalize_date(row["run_date"])

        # Build the raw data JSONB — localize timestamps
        data = {
            "Project": row["Project"] if pd.notna(row["Project"]) else None,
            "Site Name": row["Site Name"] if pd.notna(row["Site Name"]) else None,
            "Site ID": row["Site ID"] if pd.notna(row["Site ID"]) else None,
            "Task": row["Task"] if pd.notna(row["Task"]) else None,
            "Start Time": localize_timestamp(row["Start Time"]),
            "End Time": localize_timestamp(row["End Time"]),
            "Duration (min)": float(row["Duration (min)"]) if pd.notna(row["Duration (min)"]) else None,
            "User Name": row["User Name"] if pd.notna(row["User Name"]) else None,
            "User Email": row["User Email"] if pd.notna(row["User Email"]) else None,
            "User Role": row["User Role"] if pd.notna(row["User Role"]) else None,
        }

        records.append({
            "run_id": run_id,
            "source_file": SOURCE_FILE,
            "start_date": start_date,
            "end_date": end_date,
            "run_date": run_date,
            "data": data,
        })

    print(f"[{datetime.now():%H:%M:%S}] Prepared {len(records):,} raw records")
    return records


def load_raw_batches(records: list):
    """Insert raw records into raw_timer_activities_historical in batches."""
    client = create_supabase_client()
    total = len(records)
    loaded = 0

    print(f"[{datetime.now():%H:%M:%S}] Loading {total:,} records into raw_timer_activities_historical...")

    for i in range(0, total, BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        retry_supabase(
            lambda b=batch: client.schema(SCHEMA_RAW).table("raw_timer_activities_historical").insert(b).execute(),
            description=f"insert raw batch {i // BATCH_SIZE + 1}"
        )
        loaded += len(batch)
        if loaded % 10000 == 0 or loaded == total:
            print(f"[{datetime.now():%H:%M:%S}]   Raw loaded: {loaded:,} / {total:,}")

    print(f"[{datetime.now():%H:%M:%S}] Raw load complete: {loaded:,} records")
    return loaded


def get_project_did_map() -> dict:
    """Build project_name → project_did mapping from ref table.

    Fetches ALL projects (no min_project_number filter) since historical
    data spans TS5-TS18.
    """
    client = get_supabase_client()
    result = client.schema(SCHEMA_REFERENCE).table("ref_ontel_techops_projects").select(
        "project_did, project_name, project_number"
    ).order("project_number").execute()

    mapping = {row["project_name"]: row["project_did"] for row in result.data}
    print(f"[{datetime.now():%H:%M:%S}] Loaded {len(mapping)} project_did mappings")
    for name, did in sorted(mapping.items()):
        print(f"    {name} -> {did[:12]}...")
    return mapping


def verify_counts(run_id: str):
    """Verify row counts in both tables."""
    client = create_supabase_client()

    # Count raw
    raw_result = client.schema(SCHEMA_RAW).table("raw_timer_activities_historical").select("id", count="exact").eq("run_id", run_id).execute()
    raw_count = raw_result.count

    # Count staging
    stg_result = client.schema(SCHEMA_STAGING).table("stg_timer_activities").select("id", count="exact").eq("run_id", run_id).execute()
    stg_count = stg_result.count

    print(f"\n{'='*60}")
    print(f"Verification:")
    print(f"  raw_timer_activities_historical: {raw_count:,}")
    print(f"  stg_timer_activities:            {stg_count:,}")
    print(f"  Match: {'YES' if raw_count == stg_count else 'NO — MISMATCH!'}")
    print(f"{'='*60}")

    # Spot check — first and last row
    sample = client.schema(SCHEMA_STAGING).table("stg_timer_activities").select("project, project_did, start_time, end_time, duration_min, user_name").eq("run_id", run_id).order("start_time").limit(1).execute()
    if sample.data:
        row = sample.data[0]
        print(f"\n  Earliest row:")
        print(f"    Project: {row['project']}")
        print(f"    project_did: {row['project_did']}")
        print(f"    Start: {row['start_time']}")
        print(f"    End: {row['end_time']}")
        print(f"    Duration: {row['duration_min']} min")
        print(f"    User: {row['user_name']}")

    sample_last = client.schema(SCHEMA_STAGING).table("stg_timer_activities").select("project, project_did, start_time, end_time, duration_min, user_name").eq("run_id", run_id).order("start_time", desc=True).limit(1).execute()
    if sample_last.data:
        row = sample_last.data[0]
        print(f"\n  Latest row:")
        print(f"    Project: {row['project']}")
        print(f"    project_did: {row['project_did']}")
        print(f"    Start: {row['start_time']}")
        print(f"    End: {row['end_time']}")
        print(f"    Duration: {row['duration_min']} min")
        print(f"    User: {row['user_name']}")

    # Check for null project_did
    null_did = client.schema(SCHEMA_STAGING).table("stg_timer_activities").select("id", count="exact").eq("run_id", run_id).is_("project_did", "null").execute()
    null_count = null_did.count
    print(f"\n  Rows with null project_did: {null_count:,}")

    return raw_count, stg_count


def main():
    start_time = time.time()
    run_id = str(uuid.uuid4())

    print(f"{'='*60}")
    print(f"Historical Timer Activities — Bulk Load")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Run ID:  {run_id}")
    print(f"{'='*60}\n")

    # Step 1: Run migration
    print("Step 1: Running migration...")
    asyncio.run(run_migration())

    # Step 2: Read Excel
    print(f"\nStep 2: Reading Excel file...")
    df = read_excel()

    # Step 3: Prepare raw records
    print(f"\nStep 3: Preparing raw records...")
    records = prepare_raw_records(df, run_id)

    # Step 4: Load raw data
    print(f"\nStep 4: Loading raw data...")
    raw_loaded = load_raw_batches(records)

    # Free memory
    del records
    del df

    # Step 5: Get project_did mapping
    print(f"\nStep 5: Getting project_did mapping...")
    project_did_map = get_project_did_map()

    # Step 6: Transform to staging
    print(f"\nStep 6: Transforming to staging...")
    stg_loaded = transform_historical_timer_activities(run_id, project_did_map)

    # Step 7: Verify
    print(f"\nStep 7: Verification...")
    verify_counts(run_id)

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"\nTotal elapsed: {minutes}m {seconds}s")
    print(f"Done!")


if __name__ == "__main__":
    main()
