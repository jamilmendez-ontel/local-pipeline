#!/usr/bin/env python3
"""
Transform raw JSONB data into staging tables
"""

from datetime import datetime
from config import get_supabase_client


def transform_organizations(client, run_id: str):
    """Transform raw_organizations to stg_organizations"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming organizations...")

    # Fetch raw data
    result = client.table("raw_organizations").select("*").eq("run_id", run_id).execute()

    if not result.data:
        print(f"[{datetime.now():%H:%M:%S}] No organizations to transform")
        return 0

    # Clear existing staging data for this run
    client.table("stg_organizations").delete().eq("run_id", run_id).execute()

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
            "date_created": datetime.fromtimestamp(data["dateCreated"] / 1000).isoformat() if data.get("dateCreated") else None,
            "last_updated": datetime.fromtimestamp(data["lastUpdated"] / 1000).isoformat() if data.get("lastUpdated") else None,
            "run_id": run_id
        })

    # Insert in batches
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        client.table("stg_organizations").upsert(batch, on_conflict="org_did").execute()

    print(f"[{datetime.now():%H:%M:%S}] Transformed {len(rows)} organizations")
    return len(rows)


def transform_projects(client, run_id: str):
    """Transform raw_projects to stg_projects"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming projects...")

    # Clear existing staging data for this run
    client.table("stg_projects").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.table("raw_projects").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

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
                "date_created": datetime.fromtimestamp(data["dateCreated"] / 1000).isoformat() if data.get("dateCreated") else None,
                "last_updated": datetime.fromtimestamp(data["lastUpdated"] / 1000).isoformat() if data.get("lastUpdated") else None,
                "metrics_last_updated": datetime.fromtimestamp(metrics["lastUpdated"] / 1000).isoformat() if metrics.get("lastUpdated") else None,
                "run_id": run_id
            })

        client.table("stg_projects").upsert(rows, on_conflict="project_did").execute()
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
    client.table("stg_user_priorities").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches (large table)
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.table("raw_user_priorities").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]

            # Parse dates safely
            def parse_date(val):
                if not val:
                    return None
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).isoformat()
                except:
                    return None

            rows.append({
                "task_did": data.get("Task DID"),
                "asset_did": data.get("Asset DID"),
                "org_did": data.get("Organization DID"),
                "project_did": data.get("Project DID"),
                # Task info
                "task_name": data.get("Task Name"),
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

        client.table("stg_user_priorities").insert(rows).execute()
        total_transformed += len(rows)

        print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} user priorities...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total user priorities transformed: {total_transformed:,}")
    return total_transformed


def run_transform(run_id: str = None):
    """Run all transformations"""
    print(f"\n{'='*60}")
    print(f"Raw to Staging Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    client = get_supabase_client()

    # Get latest run_id if not specified
    if not run_id:
        result = client.table("pipeline_runs").select("run_id").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest run_id: {run_id}")
        else:
            print("No successful pipeline runs found")
            return

    # Transform in order (respecting FK constraints)
    org_count = transform_organizations(client, run_id)
    proj_count = transform_projects(client, run_id)
    priority_count = transform_user_priorities(client, run_id)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Organizations: {org_count:,}")
    print(f"  Projects: {proj_count:,}")
    print(f"  User Priorities: {priority_count:,}")
    print(f"{'='*60}\n")


def run_asset_tasks_transform(run_id: str = None):
    """Run asset tasks transformation only"""
    print(f"\n{'='*60}")
    print(f"Asset Tasks Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    client = get_supabase_client()

    # Get latest asset_tasks_extract run_id if not specified
    if not run_id:
        result = client.table("pipeline_runs").select("run_id").eq("pipeline_name", "asset_tasks_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
        if result.data:
            run_id = result.data[0]["run_id"]
            print(f"Using latest asset_tasks run_id: {run_id}")
        else:
            print("No successful asset_tasks pipeline runs found")
            return

    asset_count = transform_asset_tasks(client, run_id)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Asset Tasks: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_asset_tasks(client, run_id: str):
    """Transform raw_asset_tasks to stg_asset_tasks"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming asset tasks...")

    # Clear existing staging data for this run
    client.table("stg_asset_tasks").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches (large table)
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.table("raw_asset_tasks").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

        if not result.data:
            break

        rows = []
        for record in result.data:
            data = record["data"]
            project_did = record["project_did"]

            # Parse dates safely (API returns yyyy-MM-dd format, but some edge cases have epoch ms)
            def parse_date(val):
                if not val:
                    return None
                # Handle epoch milliseconds
                if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
                    try:
                        ts = int(val) / 1000 if int(val) > 9999999999 else int(val)
                        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
                    except:
                        return None
                return val  # Already in yyyy-MM-dd format from API

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
                "task_name": data.get("Task_Name"),
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

        client.table("stg_asset_tasks").insert(rows).execute()
        total_transformed += len(rows)

        if total_transformed % 10000 == 0:
            print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} asset tasks...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total asset tasks transformed: {total_transformed:,}")
    return total_transformed


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "asset_tasks":
        run_id = sys.argv[2] if len(sys.argv) > 2 else None
        run_asset_tasks_transform(run_id)
    else:
        run_transform()
