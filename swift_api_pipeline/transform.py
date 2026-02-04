#!/usr/bin/env python3
"""
Transform raw JSONB data into staging tables
"""

from datetime import datetime
from config import (
    get_supabase_client, SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE
)


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
            "date_created": datetime.fromtimestamp(data["dateCreated"] / 1000).isoformat() if data.get("dateCreated") else None,
            "last_updated": datetime.fromtimestamp(data["lastUpdated"] / 1000).isoformat() if data.get("lastUpdated") else None,
            "run_id": run_id
        })

    # Insert in batches
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        client.schema(SCHEMA_STAGING).table("stg_organizations").upsert(batch, on_conflict="org_did").execute()

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
                "date_created": datetime.fromtimestamp(data["dateCreated"] / 1000).isoformat() if data.get("dateCreated") else None,
                "last_updated": datetime.fromtimestamp(data["lastUpdated"] / 1000).isoformat() if data.get("lastUpdated") else None,
                "metrics_last_updated": datetime.fromtimestamp(metrics["lastUpdated"] / 1000).isoformat() if metrics.get("lastUpdated") else None,
                "run_id": run_id
            })

        client.schema(SCHEMA_STAGING).table("stg_projects").upsert(rows, on_conflict="project_did").execute()
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

        client.schema(SCHEMA_STAGING).table("stg_user_priorities").insert(rows).execute()
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
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("status", "success").order("started_at", desc=True).limit(1).execute()
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
        result = client.schema(SCHEMA_PIPELINE).table("pipeline_runs").select("run_id").eq("pipeline_name", "asset_tasks_extract").eq("status", "success").order("started_at", desc=True).limit(1).execute()
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
    client.schema(SCHEMA_STAGING).table("stg_asset_tasks").delete().eq("run_id", run_id).execute()

    # Fetch raw data in batches (large table)
    batch_size = 1000
    offset = 0
    total_transformed = 0

    while True:
        result = client.schema(SCHEMA_RAW).table("raw_asset_tasks").select("*").eq("run_id", run_id).range(offset, offset + batch_size - 1).execute()

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

        client.schema(SCHEMA_STAGING).table("stg_asset_tasks").insert(rows).execute()
        total_transformed += len(rows)

        if total_transformed % 10000 == 0:
            print(f"[{datetime.now():%H:%M:%S}] Transformed {total_transformed:,} asset tasks...")

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total asset tasks transformed: {total_transformed:,}")
    return total_transformed


def extract_project_number(project_name: str) -> int:
    """Extract project number from project name like 'TECH-OPS: TS13'"""
    if not project_name:
        return None
    import re
    match = re.search(r'TS(\d+)', project_name)
    return int(match.group(1)) if match else None


# QA Forms configuration (must match extract_forms.py)
QA_FORMS_CONFIG = {
    "qa_ts13": {"form_id": "-NH1hUPkaKtPdd7BK9cb", "table_name": "raw_form_qa_ts13", "display_name": "QA Form TS13"},
    "qa_ts14": {"form_id": "-NXCg4vTDNVykN8ioMYp", "table_name": "raw_form_qa_ts14", "display_name": "QA Form TS14"},
    "qa_ts15": {"form_id": "-Np6o9OCL4RWIJq68HJe", "table_name": "raw_form_qa_ts15", "display_name": "QA Form TS15"},
    "qa_ts16": {"form_id": "-O9ACLN3je1w7oEoG5hY", "table_name": "raw_form_qa_ts16", "display_name": "QA Form TS16"},
    "qa_ts17": {"form_id": "-ONMD-cGBq-_3r9ybaAq", "table_name": "raw_form_qa_ts17", "display_name": "QA Form TS17"},
    "qa_ts18": {"form_id": "-O_J2hPlryTezP9RhujA", "table_name": "raw_form_qa_ts18", "display_name": "QA Form TS18"},
}


def transform_qa_forms(client, run_id: str):
    """Transform raw QA form tables to stg_qa_form"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming QA forms...")

    # Clear existing staging data for this run
    client.schema(SCHEMA_STAGING).table("stg_qa_form").delete().eq("run_id", run_id).execute()

    total_transformed = 0
    batch_size = 1000

    for form_name, form_config in QA_FORMS_CONFIG.items():
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

                rows.append({
                    # Source tracking
                    "form_name": form_name,
                    "form_id": form_id,
                    # Project info
                    "project": project,
                    "project_number": project_number,
                    "site_name": data.get("Site Name"),
                    "site_id": data.get("Site ID"),
                    "task": data.get("Task"),
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
                client.schema(SCHEMA_STAGING).table("stg_qa_form").insert(rows).execute()
                form_count += len(rows)
                total_transformed += len(rows)

            offset += batch_size

            if len(result.data) < batch_size:
                break

        print(f"[{datetime.now():%H:%M:%S}] {display_name}: {form_count:,} rows")

    print(f"[{datetime.now():%H:%M:%S}] Total QA forms transformed: {total_transformed:,}")
    return total_transformed


def run_qa_forms_transform(run_id: str = None):
    """Run QA forms transformation only"""
    print(f"\n{'='*60}")
    print(f"QA Forms Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

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

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  QA Forms: {qa_count:,}")
    print(f"{'='*60}\n")


def transform_timer_activities(client, run_id: str):
    """Transform raw_timer_activities to stg_timer_activities"""
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

    # Delete existing staging data for this run_id (to allow re-runs)
    client.schema(SCHEMA_STAGING).table("stg_timer_activities").delete().eq("run_id", run_id).execute()

    total_transformed = 0
    batch_size = 1000
    offset = 0

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

            rows.append({
                # Project info
                "project": project,
                "project_number": project_number,
                "project_did": record["project_did"],
                # Site info
                "site_name": data.get("Site Name"),
                "site_id": data.get("Site ID"),
                "task": data.get("Task"),
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
                # Metadata
                "run_id": run_id,
                "run_date": run_date
            })

        if rows:
            client.schema(SCHEMA_STAGING).table("stg_timer_activities").insert(rows).execute()
            total_transformed += len(rows)

        offset += batch_size

        if len(result.data) < batch_size:
            break

    print(f"[{datetime.now():%H:%M:%S}] Total timer activities transformed: {total_transformed:,}")
    return total_transformed


def run_timer_transform(run_id: str = None):
    """Run timer activities transformation only"""
    print(f"\n{'='*60}")
    print(f"Timer Activities Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

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

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Timer Activities: {timer_count:,}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "asset_tasks":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_asset_tasks_transform(run_id)
        elif sys.argv[1] == "qa_forms":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_qa_forms_transform(run_id)
        elif sys.argv[1] == "timer":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_timer_transform(run_id)
        else:
            print(f"Unknown transform type: {sys.argv[1]}")
            print("Usage: python transform.py [asset_tasks|qa_forms|timer] [run_id]")
    else:
        run_transform()
