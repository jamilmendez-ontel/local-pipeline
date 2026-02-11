#!/usr/bin/env python3
"""
Transform raw JSONB data into staging tables
All timestamps are converted to America/New_York timezone for consistency
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from config import (
    get_supabase_client, create_supabase_client, SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    retry_supabase, QA_FORMS
)

# Timezone for all date conversions
TZ_ET = ZoneInfo("America/New_York")

# Regex patterns for cleaning task names
# Removes leading sequence numbers (e.g., "1. ", "10. ", "4B. ", "10B. ", "1.2. ")
# and trailing revision numbers (e.g., " 2", " 3")
TASK_NAME_PREFIX_PATTERN = re.compile(r'^(\d+[a-zA-Z]?\.\s*)+')

TASK_NAME_SUFFIX_PATTERN = re.compile(r'\s+\d+$')


def clean_task_name(task_name: str) -> str:
    """Remove sequence prefix and revision suffix from task name"""
    if not task_name:
        return None
    cleaned = TASK_NAME_PREFIX_PATTERN.sub('', task_name)
    cleaned = TASK_NAME_SUFFIX_PATTERN.sub('', cleaned)
    return cleaned.strip()


def batched_delete_all(client, schema: str, table: str, batch_size: int = 50000):
    """Delete all rows from a staging table in batches to avoid statement timeout."""
    min_result = client.schema(schema).table(table).select('id').order('id').limit(1).execute()
    max_result = client.schema(schema).table(table).select('id').order('id', desc=True).limit(1).execute()

    if not min_result.data or not max_result.data:
        return  # Table is empty

    min_id = min_result.data[0]['id']
    max_id = max_result.data[0]['id']
    current_id = min_id

    while current_id <= max_id:
        end_id = current_id + batch_size
        cid, eid = current_id, end_id
        retry_supabase(
            lambda cid=cid, eid=eid: client.schema(schema).table(table).delete().gte('id', cid).lt('id', eid).execute(),
            description=f"delete {table}"
        )
        current_id = end_id

    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from {table}")


def validate_transform_counts(client, raw_tables, stg_table, run_id, transformed_count):
    """Compare raw and staging row counts after a transform to catch silent data loss.

    Args:
        raw_tables: Single table name (str) or list of table names to sum
        stg_table: Staging table name
        run_id: Pipeline run_id to filter raw rows
        transformed_count: Number of rows the transform function reported processing
    """
    # Count raw rows for this run_id
    if isinstance(raw_tables, str):
        raw_tables = [raw_tables]

    raw_count = 0
    for table in raw_tables:
        result = client.schema(SCHEMA_RAW).table(table).select('id', count='exact').eq('run_id', run_id).limit(1).execute()
        raw_count += (result.count or 0)

    # Count staging rows
    stg_result = client.schema(SCHEMA_STAGING).table(stg_table).select('id', count='exact').limit(1).execute()
    stg_count = stg_result.count or 0

    if raw_count == transformed_count:
        status = "OK"
    else:
        status = "MISMATCH"

    print(f"  Validation [{stg_table}]: raw={raw_count:,} | transformed={transformed_count:,} | staging={stg_count:,} [{status}]")

    if status == "MISMATCH":
        print(f"  WARNING: {abs(raw_count - transformed_count):,} rows differ between raw and transformed!")


def epoch_to_datetime(epoch_ms: int) -> str:
    """Convert epoch milliseconds to ISO datetime string in America/New_York timezone"""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=TZ_ET).isoformat()


def transform_organizations(client, run_id: str):
    """Transform raw_organizations to stg_organizations"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming organizations...")

    # Fetch raw data
    result = client.schema(SCHEMA_RAW).table("raw_organizations").select("*").eq("run_id", run_id).execute()

    if not result.data:
        print(f"[{datetime.now():%H:%M:%S}] No organizations to transform")
        return 0

    # Clear existing staging data for this run
    client.schema(SCHEMA_STAGING).table("stg_organizations").delete().eq("run_id", run_id).execute()

    rows = []
    for record in result.data:
        data = record["data"]
        poc = data.get("poc", {}) or {}
        created_by = data.get("createdBy", {}) or {}

        rows.append({
            "org_did": data.get("id"),
            "org_name": data.get("name"),
            "avc": data.get("avc"),
            "poc_id": poc.get("id"),
            "poc_name": poc.get("name"),
            "poc_email": poc.get("email"),
            "created_by_id": created_by.get("id"),
            "date_created": epoch_to_datetime(data.get("dateCreated")),
            "last_updated": epoch_to_datetime(data.get("lastUpdated")),
            "run_id": run_id
        })

    # Insert in batches
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        retry_supabase(
            lambda b=batch: client.schema(SCHEMA_STAGING).table("stg_organizations").upsert(b, on_conflict="org_did").execute(),
            description="upsert stg_organizations"
        )

    print(f"[{datetime.now():%H:%M:%S}] Transformed {len(rows)} organizations")
    return len(rows)


def transform_projects(client, run_id: str):
    """Transform raw_projects to stg_projects"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming projects...")

    # Clear existing staging data for this run
    client.schema(SCHEMA_STAGING).table("stg_projects").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_projects").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            metrics = data.get("metrics", {}) or {}
            asset_metrics = metrics.get("asset", {}) or {}
            project_metrics = metrics.get("project", {}) or {}
            created_by = data.get("createdBy", {}) or {}

            rows.append({
                "project_did": data.get("id"),
                "project_name": data.get("name"),
                "org_did": data.get("_org_id"),
                "org_name": data.get("_org_name"),
                "status": data.get("status"),
                "is_private": data.get("isPrivate"),
                "location_orientation": data.get("locationOrientation"),
                # Asset metrics
                "asset_task_count": asset_metrics.get("taskCount"),
                "asset_task_pending": asset_metrics.get("taskPending"),
                "asset_task_approved": asset_metrics.get("taskApproved"),
                "asset_task_rejected": asset_metrics.get("taskRejected"),
                "asset_task_cancelled": asset_metrics.get("taskCancelled"),
                "asset_task_submitted": asset_metrics.get("taskSubmitted"),
                "asset_task_in_progress": asset_metrics.get("taskInProgress"),
                "asset_project_count": asset_metrics.get("assetProjectCount"),
                "asset_milestone_count": asset_metrics.get("milestoneCount"),
                # Project metrics
                "project_task_count": project_metrics.get("taskCount"),
                "project_task_pending": project_metrics.get("taskPending"),
                "project_task_approved": project_metrics.get("taskApproved"),
                # Metadata
                "created_by_id": created_by.get("id"),
                "date_created": epoch_to_datetime(data.get("dateCreated")),
                "last_updated": epoch_to_datetime(data.get("lastUpdated")),
                "metrics_last_updated": epoch_to_datetime(metrics.get("lastUpdated")),
                "run_id": run_id
            })

        retry_supabase(
            lambda r=rows: client.schema(SCHEMA_STAGING).table("stg_projects").upsert(r, on_conflict="project_did").execute(),
            description="upsert stg_projects"
        )
        total_transformed += len(rows)

        print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} projects...")

        offset += batch_size
        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total projects transformed: {total_transformed:,}")
    return total_transformed


def transform_user_priorities(client, run_id: str):
    """Transform raw_user_priorities to stg_user_priorities"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming user priorities...")

    # Clear existing staging data for this run
    client.schema(SCHEMA_STAGING).table("stg_user_priorities").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches (large table)
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_user_priorities").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]

            # Parse dates safely - convert to America/New_York
            def parse_date(val):
                if not val:
                    return None
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return dt.astimezone(TZ_ET).isoformat()
                except:
                    return None

            task_name = data.get("Task Name")
            rows.append({
                "task_did": data.get("Task DID"),
                "asset_did": data.get("Asset DID"),
                "org_did": data.get("Organization DID"),
                "project_did": data.get("Project DID"),
                # Task info
                "task_name": task_name,
                "task_name_clean": clean_task_name(task_name),
                "milestone": data.get("Milestone"),
                "status": data.get("Status"),
                "calendar_status": data.get("Calendar Status"),
                # Assignment
                "assigned_to": data.get("Assigned To"),
                "scheduled": parse_date(data.get("Scheduled")),
                "scheduled_by": data.get("Scheduled By"),
                "display_date": parse_date(data.get("Display Date")),
                "duration": data.get("Duration"),
                "pin_type": data.get("Pin Type"),
                # Approval workflow
                "submitted_by": data.get("Submitted By") or None,
                "submitted_on": parse_date(data.get("Submitted On")),
                "approved_by": data.get("Approved By") or None,
                "approved_on": parse_date(data.get("Approved On")),
                "rejected_by": data.get("Rejected By") or None,
                "rejected_on": parse_date(data.get("Rejected On")),
                "cancelled_by": data.get("Cancelled By") or None,
                "cancelled_on": parse_date(data.get("Cancelled On")),
                # Context
                "organization": data.get("Organization"),
                "project": data.get("Project"),
                "asset_id": data.get("Asset Id"),
                "asset_name": data.get("Asset Name"),
                # Metadata
                "run_id": run_id
            })

        retry_supabase(
            lambda r=rows: client.schema(SCHEMA_STAGING).table("stg_user_priorities").insert(r).execute(),
            description="insert stg_user_priorities"
        )
        total_transformed += len(rows)

        print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} user priorities...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total user priorities transformed: {total_transformed:,}")
    return total_transformed


def run_orgs_projects_transform(run_id: str = None, client=None):
    """Run organizations and projects transformations only"""
    print(f"\n{'='*60}")
    print(f"Organizations & Projects Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest orgs_projects_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("status", "success").eq("pipeline_name", "orgs_projects_extract").order("started_at", desc=True).limit(1).execute()
        if not result.data:
            # Fallback: try legacy pipeline name
            result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("status", "success").eq("pipeline_name", "swift_api_full_refresh").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest run_id: {run_id}")
        else:
            print("No successful orgs/projects pipeline runs found")
            return

    org_count = transform_organizations(client, run_id)
    proj_count = transform_projects(client, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_organizations", "stg_organizations", run_id, org_count)
    validate_transform_counts(client, "raw_projects", "stg_projects", run_id, proj_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Organizations: {org_count:,}")
    print(f"  Projects: {proj_count:,}")
    print(f"{'='*60}\n")


def run_user_priorities_transform(run_id: str = None, client=None):
    """Run user priorities transformation only"""
    print(f"\n{'='*60}")
    print(f"User Priorities Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest user_priorities_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("status", "success").eq("pipeline_name", "user_priorities_extract").order("started_at", desc=True).limit(1).execute()
        if not result.data:
            # Fallback: try legacy pipeline name
            result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("status", "success").eq("pipeline_name", "swift_api_full_refresh").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest run_id: {run_id}")
        else:
            print("No successful user priorities pipeline runs found")
            return

    priority_count = transform_user_priorities(client, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_user_priorities", "stg_user_priorities", run_id, priority_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  User Priorities: {priority_count:,}")
    print(f"{'='*60}\n")


def run_transform(run_id: str = None):
    """Run orgs + projects + user priorities transformations (legacy entry point)"""
    run_orgs_projects_transform(run_id)
    run_user_priorities_transform(run_id)


def run_asset_tasks_transform(run_id: str = None, client=None):
    """Run asset tasks transformation only"""
    print(f"\n{'='*60}")
    print(f"Asset Tasks Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest asset_tasks_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "asset_tasks_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest asset_tasks run_id: {run_id}")
        else:
            print("No successful asset_tasks pipeline runs found")
            return

    asset_count = transform_asset_tasks(client, run_id)

    # Row count validation
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_asset_tasks", "stg_asset_tasks", run_id, asset_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Asset Tasks: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_assets(client, run_id: str):
    """Transform raw_asset_tasks to stg_assets using SQL aggregation (RPC)"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming assets...")

    # Clear ALL existing staging data (full refresh)
    batched_delete_all(client, SCHEMA_STAGING, "stg_assets")

    # Fresh client for RPC call — avoids schema() mutation from batched_delete_all
    rpc_client = create_supabase_client()

    # Use SQL aggregation via RPC instead of scanning 2.2M rows in Python
    # Paginate results to avoid PostgREST 1000-row cap
    print(f"[{datetime.now():%H:%M:%S}] Running SQL aggregation via RPC...")
    assets_list = []
    offset = 0
    page_size = 1000
    while True:
        result = retry_supabase(
            lambda o=offset: rpc_client.schema(SCHEMA_RAW).rpc(
                "aggregate_assets_from_raw", {"p_run_id": run_id}
            ).range(o, o + page_size - 1).execute(),
            description="rpc aggregate_assets_from_raw"
        )
        batch = result.data
        assets_list.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    print(f"[{datetime.now():%H:%M:%S}] Found {len(assets_list):,} unique assets")

    # Fresh client for writes — avoids schema() thread-safety issues
    write_client = create_supabase_client()

    # Insert in batches of 2000
    insert_batch_size = 2000
    for i in range(0, len(assets_list), insert_batch_size):
        batch = assets_list[i:i + insert_batch_size]
        rows = [{**asset, "run_id": run_id} for asset in batch]
        retry_supabase(
            lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_assets").upsert(
                r, on_conflict="project_did,asset_did"
            ).execute(),
            description="upsert stg_assets"
        )

    print(f"[{datetime.now():%H:%M:%S}] Inserted {len(assets_list):,} assets")
    return len(assets_list)


def run_assets_transform(run_id: str = None, client=None):
    """Run assets transformation only"""
    print(f"\n{'='*60}")
    print(f"Assets Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest asset_tasks_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "asset_tasks_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest asset_tasks run_id: {run_id}")
        else:
            print("No successful asset_tasks pipeline runs found")
            return

    asset_count = transform_assets(client, run_id)

    # Row count validation (aggregate — staging count won't match raw 1:1)
    stg_result = client.schema(SCHEMA_STAGING).table("stg_assets").select('id', count='exact').limit(1).execute()
    print(f"\nRow Count Validation:")
    print(f"  [stg_assets]: transformed={asset_count:,} | staging={stg_result.count or 0:,} (aggregated from raw_asset_tasks)")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Assets: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_asset_tasks(client, run_id: str):
    """Transform raw_asset_tasks to stg_asset_tasks"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming asset tasks...")

    # Clear ALL existing staging data (full refresh)
    batched_delete_all(client, SCHEMA_STAGING, "stg_asset_tasks")

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    # Fetch raw data in batches (large table)
    batch_size = 1000
    offset = 0
    total_transformed = 0
    write_futures = []
    write_pool = ThreadPoolExecutor(max_workers=3)

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_asset_tasks").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            project_did = record["project_did"]

            # Parse dates safely (API returns yyyy-MM-dd format, but some edge cases have epoch ms)
            # All dates converted to America/New_York timezone
            def parse_date(val):
                if not val:
                    return None
                # Handle epoch milliseconds
                if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
                    try:
                        ts = int(val) / 1000 if int(val) > 9999999999 else int(val)
                        return datetime.fromtimestamp(ts, tz=TZ_ET).strftime('%Y-%m-%d')
                    except:
                        return None
                return val  # Already in yyyy-MM-dd format from API (requested in ET)

            task_name = data.get("Task_Name")
            rows.append({
                # DID fields
                "project_did": project_did,
                "project_status": data.get("Project_Status"),
                "asset_did": data.get("Asset_DID"),
                "task_did": data.get("Task_DID"),
                # Asset info
                "asset_id": data.get("Asset_ID"),
                "asset_name": data.get("Asset_Name"),
                "asset_requirement_count": data.get("Asset_Requirement_Count"),
                # Task info
                "task_name": task_name,
                "task_name_clean": clean_task_name(task_name),
                "task_status": data.get("Task_Status"),
                "task_scheduled": parse_date(data.get("Task_Scheduled")),
                # Assignment
                "task_assigned_to_did": data.get("Task_Assigned_To_DID"),
                "task_assigned_to_collection": data.get("Task_Assigned_To_Collection"),
                "task_assigned_to_name": data.get("Task_Assigned_To_Name"),
                "task_assigned_to_email": data.get("Task_Assigned_To_Email"),
                # Approval workflow - Submitted
                "task_submitted_on": parse_date(data.get("Task_Submitted_On")),
                "task_submitted_by_did": data.get("Task_Submitted_By_DID"),
                "task_submitted_by_name": data.get("Task_Submitted_By_Name"),
                "task_submitted_by_email": data.get("Task_Submitted_By_Email"),
                # Approval workflow - Approved
                "task_approved_on": parse_date(data.get("Task_Approved_On")),
                "task_approved_by_did": data.get("Task_Approved_By_DID"),
                "task_approved_by_name": data.get("Task_Approved_By_Name"),
                "task_approved_by_email": data.get("Task_Approved_By_Email"),
                # Approval workflow - Cancelled
                "task_cancelled_on": parse_date(data.get("Task_Cancelled_On")),
                "task_cancelled_by_did": data.get("Task_Cancelled_By_DID"),
                "task_cancelled_by_name": data.get("Task_Cancelled_By_Name"),
                "task_cancelled_by_email": data.get("Task_Cancelled_By_Email"),
                # Metadata
                "run_id": run_id
            })

        # Submit write to thread pool using separate write_client (non-blocking)
        write_futures.append(write_pool.submit(
            retry_supabase,
            lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_asset_tasks").insert(r).execute(),
            description="insert stg_asset_tasks"
        ))
        total_transformed += len(rows)

        if total_transformed % 10000 == 0:
            print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} asset tasks...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    # Wait for all writes to complete
    for f in write_futures:
        f.result()  # raises if any write failed
    write_pool.shutdown(wait=False)

    print(f"[{datetime.now():%H:%M:%S}] Total asset tasks transformed: {total_transformed:,}")
    return total_transformed


def extract_project_number(project_name: str) -> int:
    """Extract project number from project name like 'TECH-OPS: TS13'"""
    if not project_name:
        return None
    import re
    match = re.search(r'TS(\d+)', project_name)
    return int(match.group(1)) if match else None




def transform_qa_forms(client, run_id: str):
    """Transform raw QA form tables to stg_qa_form"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming QA forms...")

    # Clear ALL existing staging data (full refresh)
    batched_delete_all(client, SCHEMA_STAGING, "stg_qa_form")

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    total_transformed = 0
    batch_size = 1000
    write_futures = []
    write_pool = ThreadPoolExecutor(max_workers=3)

    for form_name, form_config in QA_FORMS.items():
        table_name = form_config["table_name"]
        form_id = form_config["form_id"]
        display_name = form_config["display_name"]

        print(f"[{datetime.now():%H:%M:%S}] Processing {display_name}...")

        offset = 0
        form_count = 0

        while True:
            result = client.schema(SCHEMA_RAW).table(table_name).select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

            if not result.data:
                break

            rows = []
            for record in result.data:
                data = record["data"]
                project = data.get("Project", "")
                project_number = extract_project_number(project)

                # Helper to get value with case variations
                def get_val(*keys):
                    for k in keys:
                        v = data.get(k)
                        if v:
                            return v
                    return data.get(keys[0])

                task = data.get("Task")
                rows.append({
                    # Source tracking
                    "form_name": form_name,
                    "form_id": form_id,
                    # Project info
                    "project": project,
                    "project_number": project_number,
                    "site_name": data.get("Site Name"),
                    "site_id": data.get("Site ID"),
                    "task": task,
                    "task_clean": clean_task_name(task),
                    "requirement": data.get("Requirement"),
                    "requirement_status": data.get("Requirement Status"),
                    # QA fields
                    "live_review_performed": data.get("Live Review Performed"),
                    "swift_used_for_photos": data.get("Swift Used for Photos"),
                    "crew_lead": data.get("Crew Lead"),
                    # Construction & Personnel
                    "construction_manager": data.get("Construction Manager (CM)"),
                    "subcontractor": data.get("Subcontractor (if applicable)"),
                    # AAT
                    "aat": data.get("AAT"),
                    "aat_issues": data.get("AAT Issues"),
                    "aat_other_issues": data.get("AAT (Other issues)"),
                    # RET
                    "ret": data.get("RET"),
                    "ret_issues": data.get("RET Issues"),
                    "ret_other_issues": data.get("RET (Others issues)"),
                    "ret_values": data.get("RET Values"),
                    "ret_visibility": data.get("RET Visibility"),
                    # Sweeps
                    "sweeps": data.get("Sweeps"),
                    "sweeps_issues": data.get("Sweeps Issues"),
                    "sweeps_other_issues": data.get("Sweeps (Other issues)"),
                    # PIM
                    "pim": data.get("PIM"),
                    "pim_issues": data.get("PIM Issues"),
                    "pim_other_issues": data.get("PIM (Other issues)"),
                    # Fiber
                    "fiber": data.get("Fiber"),
                    "fiber_issues": data.get("Fiber Issues"),
                    "fiber_other_issues": data.get("Fiber (Other issues)"),
                    # Pictures
                    "pictures": data.get("Pictures"),
                    "pictures_issues": data.get("Pictures Issues"),
                    "pictures_other_issues": data.get("Pictures (Other issues)"),
                    # Sector & Photos
                    "sector_photos": data.get("Sector Photos"),
                    "powershift_photos": data.get("Powershift Photos"),
                    # As-Builts (handle case variation)
                    "as_builts": data.get("As-Builts"),
                    "as_builts_issues": data.get("As-Builts Issues"),
                    "as_builts_other_issues": get_val("As-Builts (Other issues)", "AS-Builts (Other issues)"),
                    # RF Mitigation
                    "rf_mitigation": data.get("RF Mitigation"),
                    "rf_mitigation_issues": data.get("RF Mitigation Issues"),
                    "rf_mitigation_other_issues": data.get("RF Mitigation (Other issues)"),
                    # Landlord / Tower Owner
                    "landlord_tower_owner": data.get("Landlord / Tower Owner"),
                    "landlord_tower_owner_issues": data.get("Landlord / Tower Owner Issues"),
                    "other_landlord_photos": data.get("Other Landlord-related photos"),
                    # Permits
                    "permits": data.get("Permits"),
                    # Additional Documents
                    "additional_documents": data.get("Additional Documents (if applicable)"),
                    # PMI
                    "pmi": data.get("PMI (if applicable)"),
                    "pmi_vendor": data.get("(PMI) Vendor Antenna Mount Structural Company"),
                    "pmi_others_vendor": get_val("Others (PMI Vendor):", "Others (PMI Vendor)"),
                    "pmi_mount_modification_required": data.get("(PMI) Mount Modification Required?"),
                    "pmi_issues": data.get("PMI Issues"),
                    "pmi_other_issues": data.get("PMI (Other issues)"),
                    "pmi_report_received": data.get("(PMI) Post Modification Inspection Report received?"),
                    "signed_pmi_report": data.get("Signed PMI Report"),
                    "material_packing_signed_pmi": data.get("Material Packing List, Signed PMI Report"),
                    # Power Testing (handle case variation)
                    "power_testing": data.get("Power Testing (if applicable)"),
                    "power_testing_issues": data.get("Power Testing Issues"),
                    "power_testing_other_issues": get_val("Power Testing (Other Issues)", "Power Testing (Other issues)"),
                    # Connectivity Testing
                    "connectivity_testing": data.get("Connectivity Testing (if applicable)"),
                    "connectivity_testing_issues": data.get("Connectivity Testing Issues"),
                    "connectivity_testing_other_issues": data.get("Connectivity Testing (Other Issues)"),
                    # Optical Power Testing (handle case variation)
                    "optical_power_testing": data.get("Optical Power Testing (if applicable)"),
                    "optical_power_testing_other_issues": get_val("Optical Power Testing (Other Issues)", "Optical Power Testing (Other issues)"),
                    # Restoration
                    "restoration": data.get("Restoration (if applicable)"),
                    # NA Checklist
                    "na_checklist": data.get("NA Checklist (if applicable)"),
                    "na_checklist_issues": data.get("N/A Checklist Issues"),
                    "na_checklist_other_issues": data.get("N/A Checklist (Other Issues)"),
                    # RCM
                    "rcm_approval": data.get("RCM approval"),
                    # Completeness (handle variations)
                    "completeness_of_files": get_val("Completeness of files", "Completeness of Files"),
                    # Serials & Labels
                    "serials": data.get("Serials"),
                    "font_size_of_labels": data.get("Font Size of Labels"),
                    "labels_sector_tape": data.get("Labels (P-touch, Marks, Tags), Sector Photos, Tape Drop"),
                    # Smart Level & Calibration
                    "smart_level": data.get("Smart Level (Plumb and MDT)"),
                    "calibration_details": data.get("Calibration Details"),
                    # General Ground
                    "general_ground": data.get("General Ground"),
                    # Conditional Pass
                    "conditional_pass": data.get("Conditional Pass"),
                    # Supports
                    "supports": data.get("Supports (i.e. Snap-In, etc.)"),
                    # Metadata
                    "run_id": run_id
                })

            if rows:
                # Submit write to thread pool using separate write_client (non-blocking)
                write_futures.append(write_pool.submit(
                    retry_supabase,
                    lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_qa_form").insert(r).execute(),
                    description="insert stg_qa_form"
                ))
                form_count += len(rows)
                total_transformed += len(rows)

            offset += batch_size

            if len(result.data) < batch_size:
                break

        print(f"[{datetime.now():%H:%M:%S}] {display_name}: {form_count:,} rows")

    # Wait for all writes to complete
    for f in write_futures:
        f.result()
    write_pool.shutdown(wait=False)

    print(f"[{datetime.now():%H:%M:%S}] Total QA forms transformed: {total_transformed:,}")
    return total_transformed


def run_qa_forms_transform(run_id: str = None, client=None):
    """Run QA forms transformation only"""
    print(f"\n{'='*60}")
    print(f"QA Forms Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest forms_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "forms_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest forms run_id: {run_id}")
        else:
            print("No successful forms pipeline runs found")
            return

    qa_count = transform_qa_forms(client, run_id)

    # Row count validation (sum across all raw form tables)
    raw_tables = [cfg["table_name"] for cfg in QA_FORMS.values()]
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, raw_tables, "stg_qa_form", run_id, qa_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  QA Forms: {qa_count:,}")
    print(f"{'='*60}\n")


def transform_timer_activities(client, run_id: str):
    """Transform raw_timer_activities to stg_timer_activities (append mode - preserves all runs)"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming timer activities...")

    # Get run metadata
    raw_result = client.schema(SCHEMA_RAW).table("raw_timer_activities").select("run_date, start_date, end_date").eq("run_id", run_id).limit(1).execute()
    if not raw_result.data:
        print(f"[{datetime.now():%H:%M:%S}] No timer data found for run_id: {run_id}")
        return 0

    run_date = raw_result.data[0]["run_date"]
    start_date = raw_result.data[0]["start_date"]
    end_date = raw_result.data[0]["end_date"]

    print(f"[{datetime.now():%H:%M:%S}] Run date: {run_date}, Date range: {start_date} to {end_date}")

    # Delete existing staging data for this run_id only (allows re-runs without duplicates)
    # Data from other run_ids is preserved (append mode)
    client.schema(SCHEMA_STAGING).table("stg_timer_activities").delete().eq("run_id", run_id).execute()

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    total_transformed = 0
    batch_size = 1000
    offset = 0
    write_futures = []
    write_pool = ThreadPoolExecutor(max_workers=3)

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_timer_activities").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            project = data.get("Project", "")
            project_number = extract_project_number(project)

            # Parse timestamps
            start_time = data.get("Start Time")
            end_time = data.get("End Time")

            task = data.get("Task")
            rows.append({
                # Project info
                "project": project,
                "project_number": project_number,
                "project_did": record["project_did"],
                # Site info
                "site_name": data.get("Site Name"),
                "site_id": data.get("Site ID"),
                "task": task,
                "task_clean": clean_task_name(task),
                # Location data
                "site_lat": data.get("Site Lat"),
                "site_long": data.get("Site Long"),
                "user_lat": data.get("User Lat"),
                "user_long": data.get("User Long"),
                "user_accuracy_m": data.get("User Accuracy (m)"),
                "site_vs_user_km": data.get("Site vs User (km)"),
                # Time data
                "start_time": start_time,
                "end_time": end_time,
                "duration_min": data.get("Duration (min)"),
                # User info
                "user_name": data.get("User Name"),
                "user_email": data.get("User Email"),
                "user_role": data.get("User Role"),
                # Metadata (matching raw table structure)
                "run_id": run_id,
                "run_date": run_date,
                "start_date": start_date,
                "end_date": end_date
            })

        if rows:
            write_futures.append(write_pool.submit(
                retry_supabase,
                lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_timer_activities").insert(r).execute(),
                description="insert stg_timer_activities"
            ))
            total_transformed += len(rows)

        offset += batch_size

        if len(result.data) < batch_size:
            break

    # Wait for all writes to complete
    for f in write_futures:
        f.result()
    write_pool.shutdown(wait=False)

    print(f"[{datetime.now():%H:%M:%S}] Total timer activities transformed: {total_transformed:,}")
    return total_transformed


def run_timer_transform(run_id: str = None, client=None):
    """Run timer activities transformation only"""
    print(f"\n{'='*60}")
    print(f"Timer Activities Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest timer_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "timer_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest timer run_id: {run_id}")
        else:
            print("No successful timer pipeline runs found")
            return

    timer_count = transform_timer_activities(client, run_id)

    # Row count validation (append mode — validates current run only)
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_timer_activities", "stg_timer_activities", run_id, timer_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Timer Activities: {timer_count:,}")
    print(f"{'='*60}\n")


def transform_historical_timer_activities(run_id: str, project_did_map: dict):
    """Transform raw_timer_activities_historical → stg_timer_activities.

    Used by load_historical_timer.py to transform Excel-sourced historical
    timer data into the same staging table used by the regular pipeline.

    Args:
        run_id: UUID identifying this historical load
        project_did_map: dict mapping project_name → project_did
    """
    read_client = create_supabase_client()
    write_client = create_supabase_client()

    # Delete any existing staging data for this run_id (idempotent re-runs)
    write_client.schema(SCHEMA_STAGING).table("stg_timer_activities").delete().eq("run_id", run_id).execute()

    total_transformed = 0
    batch_size = 1000
    offset = 0
    write_futures = []
    write_pool = ThreadPoolExecutor(max_workers=3)
    missing_projects = set()

    print(f"[{datetime.now():%H:%M:%S}] Transforming historical timer data to staging...")

    while True:
        result = read_client.schema(SCHEMA_RAW).table("raw_timer_activities_historical").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            project = data.get("Project", "")
            project_number = extract_project_number(project)
            project_did = project_did_map.get(project)

            if project_did is None and project not in missing_projects:
                missing_projects.add(project)
                print(f"  WARNING: No project_did for '{project}'")

            task = data.get("Task")

            rows.append({
                "project": project,
                "project_number": project_number,
                "project_did": project_did,
                "site_name": data.get("Site Name"),
                "site_id": data.get("Site ID"),
                "task": task,
                "task_clean": clean_task_name(task),
                # GPS columns — not in Excel data
                "site_lat": None,
                "site_long": None,
                "user_lat": None,
                "user_long": None,
                "user_accuracy_m": None,
                "site_vs_user_km": None,
                # Time data
                "start_time": data.get("Start Time"),
                "end_time": data.get("End Time"),
                "duration_min": data.get("Duration (min)"),
                # User info
                "user_name": data.get("User Name"),
                "user_email": data.get("User Email"),
                "user_role": data.get("User Role"),
                # Metadata
                "run_id": run_id,
                "run_date": str(record["run_date"]),
                "start_date": str(record["start_date"]),
                "end_date": str(record["end_date"]),
            })

        if rows:
            write_futures.append(write_pool.submit(
                retry_supabase,
                lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_timer_activities").insert(r).execute(),
                description="insert stg_timer historical batch"
            ))
            total_transformed += len(rows)

        offset += batch_size

        if total_transformed % 10000 == 0 and total_transformed > 0:
            print(f"[{datetime.now():%H:%M:%S}]   Staging transformed: {total_transformed:,}")

        if len(result.data) < batch_size:
            break

    # Wait for all writes
    for f in write_futures:
        f.result()
    write_pool.shutdown(wait=False)

    print(f"[{datetime.now():%H:%M:%S}] Historical timer staging complete: {total_transformed:,} records")
    return total_transformed


def transform_ar_aging(client, run_id: str):
    """Transform raw_ar_aging to stg_ar_aging for a specific run_id (append mode)."""
    print(f"[{datetime.now():%H:%M:%S}] Transforming AR aging...")

    # Delete existing staging data for this run_id (idempotent re-runs)
    client.schema(SCHEMA_STAGING).table("stg_ar_aging").delete().eq("run_id", run_id).execute()

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    total_transformed = 0
    batch_size = 1000
    offset = 0

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_ar_aging").select("*").eq(
            "run_id", run_id
        ).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            rows.append({
                "as_of_date": record["as_of_date"],
                "email_received_date": record.get("email_received_date"),
                "aging_bucket": data.get("aging_bucket"),
                "date": data.get("date"),
                "transaction_type": data.get("transaction_type"),
                "num": data.get("num"),
                "customer": data.get("customer"),
                "location": data.get("location"),
                "due_date": data.get("due_date"),
                "amount": data.get("amount"),
                "open_balance": data.get("open_balance"),
                "past_due": data.get("past_due"),
                "po_number": data.get("po_number"),
                "run_id": run_id,
            })

        if rows:
            retry_supabase(
                lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_ar_aging").insert(r).execute(),
                description="insert stg_ar_aging"
            )
            total_transformed += len(rows)

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total AR aging records transformed: {total_transformed:,}")
    return total_transformed


def run_ar_aging_transform(run_id: str = None, client=None):
    """Run AR aging transformation only."""
    print(f"\n{'='*60}")
    print(f"AR Aging Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest ar_aging_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select(
            "run_id"
        ).eq("pipeline_name", "ar_aging_extract").eq("status", "success").order(
            "started_at", desc=True
        ).limit(1).execute()

        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest ar_aging run_id: {run_id}")
        else:
            print("No successful AR aging pipeline runs found")
            return

    aging_count = transform_ar_aging(client, run_id)

    # Row count validation
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_ar_aging", "stg_ar_aging", run_id, aging_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  AR Aging Records: {aging_count:,}")
    print(f"{'='*60}\n")


def transform_sales_detail(client, run_id: str):
    """Transform raw_sales_detail to stg_sales_detail for a specific run_id (append mode)."""
    print(f"[{datetime.now():%H:%M:%S}] Transforming sales detail...")

    # Delete existing staging data for this run_id (idempotent re-runs)
    client.schema(SCHEMA_STAGING).table("stg_sales_detail").delete().eq("run_id", run_id).execute()

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    total_transformed = 0
    batch_size = 1000
    offset = 0

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_sales_detail").select("*").eq(
            "run_id", run_id
        ).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            rows.append({
                "as_of_date": record["as_of_date"],
                "email_received_date": record.get("email_received_date"),
                "date": data.get("date"),
                "transaction_type": data.get("transaction_type"),
                "num": data.get("num"),
                "customer": data.get("customer"),
                "memo_description": data.get("memo_description"),
                "qty": data.get("qty"),
                "sales_price": data.get("sales_price"),
                "amount": data.get("amount"),
                "balance": data.get("balance"),
                "po_number": data.get("po_number"),
                "service_date": data.get("service_date"),
                "run_id": run_id,
            })

        if rows:
            retry_supabase(
                lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_sales_detail").insert(r).execute(),
                description="insert stg_sales_detail"
            )
            total_transformed += len(rows)

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total sales detail records transformed: {total_transformed:,}")
    return total_transformed


def run_sales_detail_transform(run_id: str = None, client=None):
    """Run sales detail transformation only."""
    print(f"\n{'='*60}")
    print(f"Sales Detail Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = get_supabase_client()

    # Get latest sales_detail_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select(
            "run_id"
        ).eq("pipeline_name", "sales_detail_extract").eq("status", "success").order(
            "started_at", desc=True
        ).limit(1).execute()

        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest sales_detail run_id: {run_id}")
        else:
            print("No successful sales detail pipeline runs found")
            return

    sales_count = transform_sales_detail(client, run_id)

    # Row count validation
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_sales_detail", "stg_sales_detail", run_id, sales_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Sales Detail Records: {sales_count:,}")
    print(f"{'='*60}\n")


def transform_requirements(client, run_id: str):
    """Transform raw_asset_task_requirements to stg_asset_task_requirements"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming requirements...")

    # Clear ALL existing staging data (full refresh)
    batched_delete_all(client, SCHEMA_STAGING, "stg_asset_task_requirements")

    # Separate write client to avoid schema() thread-safety issues
    write_client = create_supabase_client()

    total_transformed = 0
    batch_size = 1000
    offset = 0
    write_futures = []
    write_pool = ThreadPoolExecutor(max_workers=3)

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_asset_task_requirements").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            project_did = record["project_did"]
            task_did = record["task_did"]

            # Extract nested objects safely
            assigned_to = data.get("assignedTo") or {}
            completed_by = data.get("completedBy") or {}
            submitted_by = data.get("submittedBy") or {}
            approved_by = data.get("approvedBy") or {}
            rejected_by = data.get("rejectedBy") or {}

            rows.append({
                # Hierarchy identifiers
                "project_did": project_did,
                "asset_did": data.get("_asset_did"),
                "task_did": task_did,
                "requirement_did": data.get("id") or data.get("_id"),
                # Requirement info
                "requirement_name": data.get("name"),
                "requirement_type": data.get("type") or data.get("requirementType"),
                "requirement_status": data.get("status"),
                "requirement_description": data.get("description"),
                # Media/attachments
                "has_photo": data.get("hasPhoto"),
                "has_document": data.get("hasDocument"),
                "photo_count": data.get("photoCount"),
                "document_count": data.get("documentCount"),
                # Assignment
                "assigned_to_did": assigned_to.get("id"),
                "assigned_to_name": assigned_to.get("name"),
                "assigned_to_email": assigned_to.get("email"),
                # Completion
                "completed_by_did": completed_by.get("id"),
                "completed_by_name": completed_by.get("name"),
                "completed_on": epoch_to_datetime(data.get("completedOn")),
                # Submission
                "submitted_on": epoch_to_datetime(data.get("submittedOn")),
                "submitted_by_did": submitted_by.get("id"),
                "submitted_by_name": submitted_by.get("name"),
                # Approval
                "approved_on": epoch_to_datetime(data.get("approvedOn")),
                "approved_by_did": approved_by.get("id"),
                "approved_by_name": approved_by.get("name"),
                # Rejection
                "rejected_on": epoch_to_datetime(data.get("rejectedOn")),
                "rejected_by_did": rejected_by.get("id"),
                "rejected_by_name": rejected_by.get("name"),
                # Form data
                "form_id": data.get("formId"),
                "form_response_id": data.get("formResponseId"),
                # Metadata
                "date_created": epoch_to_datetime(data.get("dateCreated")),
                "last_updated": epoch_to_datetime(data.get("lastUpdated")),
                "run_id": run_id
            })

        if rows:
            write_futures.append(write_pool.submit(
                retry_supabase,
                lambda r=rows: write_client.schema(SCHEMA_STAGING).table("stg_asset_task_requirements").insert(r).execute(),
                description="insert stg_asset_task_requirements"
            ))
            total_transformed += len(rows)

        if total_transformed % 10000 == 0 and total_transformed > 0:
            print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} requirements...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    # Wait for all writes to complete
    for f in write_futures:
        f.result()
    write_pool.shutdown(wait=False)

    print(f"[{datetime.now():%H:%M:%S}] Total requirements transformed: {total_transformed:,}")
    return total_transformed


def run_requirements_transform(run_id: str = None):
    """Run requirements transformation only"""
    print(f"\n{'='*60}")
    print(f"Requirements Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    client = get_supabase_client()

    # Get latest requirements_extract run_id if not specified
    if not run_id:
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "requirements_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest requirements run_id: {run_id}")
        else:
            print("No successful requirements pipeline runs found")
            return

    req_count = transform_requirements(client, run_id)

    # Row count validation
    print(f"\nRow Count Validation:")
    validate_transform_counts(client, "raw_asset_task_requirements", "stg_asset_task_requirements", run_id, req_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Requirements: {req_count:,}")
    print(f"{'='*60}\n")


def backfill_asset_did(client=None):
    """Backfill asset_did on stg_timer_activities and stg_qa_form from stg_assets.

    Calls the data_staging.backfill_asset_did() RPC which runs server-side
    UPDATEs joining on site_id = asset_id. Self-healing: each run refreshes
    asset_did for ALL rows, catching any asset_id changes.
    """
    print(f"\n{'='*60}")
    print(f"Asset DID Backfill")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    if client is None:
        client = create_supabase_client()

    # Verify stg_assets has data before calling RPC
    check = client.schema(SCHEMA_STAGING).table("stg_assets").select("asset_did", count="exact").limit(1).execute()
    if not check.count:
        print("stg_assets is empty -- skipping asset_did backfill")
        return

    result = client.schema(SCHEMA_STAGING).rpc("backfill_asset_did", params={}).execute()

    if result.data:
        row = result.data[0]
        timer_updated = row.get("timer_updated", 0)
        qa_form_updated = row.get("qa_form_updated", 0)
        print(f"  Timer rows updated:   {timer_updated:,}")
        print(f"  QA Form rows updated: {qa_form_updated:,}")
    else:
        print("  RPC returned no data")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "assets":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_assets_transform(run_id)
        elif sys.argv[1] == "asset_tasks":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_asset_tasks_transform(run_id)
        elif sys.argv[1] == "qa_forms":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_qa_forms_transform(run_id)
        elif sys.argv[1] == "timer":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_timer_transform(run_id)
        elif sys.argv[1] == "requirements":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_requirements_transform(run_id)
        elif sys.argv[1] == "ar_aging":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_ar_aging_transform(run_id)
        elif sys.argv[1] == "sales":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_sales_detail_transform(run_id)
        else:
            print(f"Unknown transform type: {sys.argv[1]}")
            print("Usage: python transform.py [assets|asset_tasks|qa_forms|timer|requirements|ar_aging|sales] [run_id]")
    else:
        run_transform()
