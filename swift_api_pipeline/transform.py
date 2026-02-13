#!/usr/bin/env python3
"""
Transform raw JSONB data into staging tables
All timestamps are converted to America/New_York timezone for consistency
"""

import re
import json
from datetime import datetime, date as _date
from zoneinfo import ZoneInfo
from config import (
    get_db, SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    retry_db, QA_FORMS, get_logger
)

logger = get_logger("transform")

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


def parse_date(val) -> _date:
    """Convert string/date to datetime.date for asyncpg parameterized queries. Returns None for None/empty."""
    if val is None or val == '':
        return None
    if isinstance(val, _date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    return _date.fromisoformat(str(val))


def parse_timestamp(val) -> datetime:
    """Convert string/datetime to datetime for asyncpg parameterized queries. Returns None for None/empty."""
    if val is None or val == '':
        return None
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(str(val))


def validate_transform_counts(db, raw_tables, stg_table, run_id, transformed_count):
    """Compare raw and staging row counts after a transform to catch silent data loss."""
    if isinstance(raw_tables, str):
        raw_tables = [raw_tables]

    raw_count = 0
    for table in raw_tables:
        count = db.fetchval(
            f'SELECT COUNT(*) FROM {SCHEMA_RAW}.{table} WHERE run_id = $1',
            run_id
        )
        raw_count += count

    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.{stg_table}')

    if raw_count == transformed_count:
        status = "OK"
    else:
        status = "MISMATCH"

    print(f"  Validation [{stg_table}]: raw={raw_count:,} | transformed={transformed_count:,} | staging={stg_count:,} [{status}]")

    if status == "MISMATCH":
        print(f"  WARNING: {abs(raw_count - transformed_count):,} rows differ between raw and transformed!")


def epoch_to_datetime(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware datetime in America/New_York."""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=TZ_ET)


def transform_organizations(db, run_id: str):
    """Transform raw_organizations to stg_organizations"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming organizations...")

    # Fetch raw data — single query, no pagination needed
    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_organizations WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No organizations to transform")
        return 0

    # Clear existing staging data for this run
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_organizations WHERE run_id = $1',
        run_id
    )

    rows = []
    for record in result:
        data = record["data"]
        poc = data.get("poc", {}) or {}
        created_by = data.get("createdBy", {}) or {}

        rows.append((
            data.get("id"),
            data.get("name"),
            data.get("avc"),
            poc.get("id"),
            poc.get("name"),
            poc.get("email"),
            created_by.get("id"),
            epoch_to_datetime(data.get("dateCreated")),
            epoch_to_datetime(data.get("lastUpdated")),
            run_id
        ))

    db.executemany(
        f'INSERT INTO {SCHEMA_STAGING}.stg_organizations '
        f'(org_did, org_name, avc, poc_id, poc_name, poc_email, created_by_id, '
        f'date_created, last_updated, run_id) '
        f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) '
        f'ON CONFLICT (org_did) DO UPDATE SET '
        f'org_name=EXCLUDED.org_name, avc=EXCLUDED.avc, poc_id=EXCLUDED.poc_id, '
        f'poc_name=EXCLUDED.poc_name, poc_email=EXCLUDED.poc_email, '
        f'created_by_id=EXCLUDED.created_by_id, date_created=EXCLUDED.date_created, '
        f'last_updated=EXCLUDED.last_updated, run_id=EXCLUDED.run_id',
        rows
    )

    print(f"[{datetime.now():%H:%M:%S}] Transformed {len(rows)} organizations")
    return len(rows)


def transform_projects(db, run_id: str):
    """Transform raw_projects to stg_projects"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming projects...")

    # Clear existing staging data for this run
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_projects WHERE run_id = $1',
        run_id
    )

    # Fetch all raw data — single query
    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_projects WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No projects to transform")
        return 0

    rows = []
    for record in result:
        data = record["data"]
        metrics = data.get("metrics", {}) or {}
        asset_metrics = metrics.get("asset", {}) or {}
        project_metrics = metrics.get("project", {}) or {}
        created_by = data.get("createdBy", {}) or {}

        rows.append((
            data.get("id"),
            data.get("name"),
            data.get("_org_id"),
            data.get("_org_name"),
            data.get("status"),
            data.get("isPrivate"),
            data.get("locationOrientation"),
            asset_metrics.get("taskCount"),
            asset_metrics.get("taskPending"),
            asset_metrics.get("taskApproved"),
            asset_metrics.get("taskRejected"),
            asset_metrics.get("taskCancelled"),
            asset_metrics.get("taskSubmitted"),
            asset_metrics.get("taskInProgress"),
            asset_metrics.get("assetProjectCount"),
            asset_metrics.get("milestoneCount"),
            project_metrics.get("taskCount"),
            project_metrics.get("taskPending"),
            project_metrics.get("taskApproved"),
            created_by.get("id"),
            epoch_to_datetime(data.get("dateCreated")),
            epoch_to_datetime(data.get("lastUpdated")),
            epoch_to_datetime(metrics.get("lastUpdated")),
            run_id
        ))

    db.executemany(
        f'INSERT INTO {SCHEMA_STAGING}.stg_projects '
        f'(project_did, project_name, org_did, org_name, status, is_private, '
        f'location_orientation, asset_task_count, asset_task_pending, asset_task_approved, '
        f'asset_task_rejected, asset_task_cancelled, asset_task_submitted, asset_task_in_progress, '
        f'asset_project_count, asset_milestone_count, project_task_count, project_task_pending, '
        f'project_task_approved, created_by_id, date_created, last_updated, metrics_last_updated, '
        f'run_id) '
        f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24) '
        f'ON CONFLICT (project_did) DO UPDATE SET '
        f'project_name=EXCLUDED.project_name, org_did=EXCLUDED.org_did, org_name=EXCLUDED.org_name, '
        f'status=EXCLUDED.status, is_private=EXCLUDED.is_private, '
        f'location_orientation=EXCLUDED.location_orientation, asset_task_count=EXCLUDED.asset_task_count, '
        f'asset_task_pending=EXCLUDED.asset_task_pending, asset_task_approved=EXCLUDED.asset_task_approved, '
        f'asset_task_rejected=EXCLUDED.asset_task_rejected, asset_task_cancelled=EXCLUDED.asset_task_cancelled, '
        f'asset_task_submitted=EXCLUDED.asset_task_submitted, asset_task_in_progress=EXCLUDED.asset_task_in_progress, '
        f'asset_project_count=EXCLUDED.asset_project_count, asset_milestone_count=EXCLUDED.asset_milestone_count, '
        f'project_task_count=EXCLUDED.project_task_count, project_task_pending=EXCLUDED.project_task_pending, '
        f'project_task_approved=EXCLUDED.project_task_approved, created_by_id=EXCLUDED.created_by_id, '
        f'date_created=EXCLUDED.date_created, last_updated=EXCLUDED.last_updated, '
        f'metrics_last_updated=EXCLUDED.metrics_last_updated, run_id=EXCLUDED.run_id',
        rows
    )

    print(f"[{datetime.now():%H:%M:%S}] Total projects transformed: {len(rows):,}")
    return len(rows)


def transform_user_priorities(db, run_id: str):
    """Transform raw_user_priorities to stg_user_priorities"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming user priorities...")

    # Clear ALL existing staging data (full refresh)
    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_user_priorities')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_user_priorities")

    # Fetch all raw data — single query
    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_user_priorities WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No user priorities to transform")
        return 0

    def parse_ts(val):
        """Parse ISO datetime string to timezone-aware datetime in ET."""
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.astimezone(TZ_ET)
        except Exception:
            return None

    rows = []
    for record in result:
        data = record["data"]

        task_name = data.get("Task Name")
        rows.append((
            data.get("Task DID"),
            data.get("Asset DID"),
            data.get("Organization DID"),
            data.get("Project DID"),
            task_name,
            clean_task_name(task_name),
            data.get("Milestone"),
            data.get("Status"),
            data.get("Calendar Status"),
            data.get("Assigned To"),
            parse_ts(data.get("Scheduled")),
            data.get("Scheduled By"),
            parse_ts(data.get("Display Date")),
            data.get("Duration"),
            data.get("Pin Type"),
            data.get("Submitted By") or None,
            parse_ts(data.get("Submitted On")),
            data.get("Approved By") or None,
            parse_ts(data.get("Approved On")),
            data.get("Rejected By") or None,
            parse_ts(data.get("Rejected On")),
            data.get("Cancelled By") or None,
            parse_ts(data.get("Cancelled On")),
            data.get("Organization"),
            data.get("Project"),
            data.get("Asset Id"),
            data.get("Asset Name"),
            run_id
        ))

    # Insert in batches via executemany
    batch_size = 5000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        db.executemany(
            f'INSERT INTO {SCHEMA_STAGING}.stg_user_priorities '
            f'(task_did, asset_did, org_did, project_did, task_name, task_name_clean, '
            f'milestone, status, calendar_status, assigned_to, scheduled, scheduled_by, '
            f'display_date, duration, pin_type, submitted_by, submitted_on, approved_by, '
            f'approved_on, rejected_by, rejected_on, cancelled_by, cancelled_on, '
            f'organization, project, asset_id, asset_name, run_id) '
            f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)',
            batch
        )

    total = len(rows)
    print(f"[{datetime.now():%H:%M:%S}] Total user priorities transformed: {total:,}")
    return total


def run_orgs_projects_transform(run_id: str = None, client=None):
    """Run organizations and projects transformations only"""
    print(f"\n{'='*60}")
    print(f"Organizations & Projects Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE status = $1 AND pipeline_name = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "success", "orgs_projects_extract"
        )
        if not row:
            row = db.fetchrow(
                f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
                f'WHERE status = $1 AND pipeline_name = $2 '
                f'ORDER BY started_at DESC LIMIT 1',
                "success", "swift_api_full_refresh"
            )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest run_id: {run_id}")
        else:
            print("No successful orgs/projects pipeline runs found")
            return

    org_count = transform_organizations(db, run_id)
    proj_count = transform_projects(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_organizations", "stg_organizations", run_id, org_count)
    validate_transform_counts(db, "raw_projects", "stg_projects", run_id, proj_count)

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

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE status = $1 AND pipeline_name = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "success", "user_priorities_extract"
        )
        if not row:
            row = db.fetchrow(
                f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
                f'WHERE status = $1 AND pipeline_name = $2 '
                f'ORDER BY started_at DESC LIMIT 1',
                "success", "swift_api_full_refresh"
            )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest run_id: {run_id}")
        else:
            print("No successful user priorities pipeline runs found")
            return

    priority_count = transform_user_priorities(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_user_priorities", "stg_user_priorities", run_id, priority_count)

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

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks run_id: {run_id}")
        else:
            print("No successful asset_tasks pipeline runs found")
            return

    asset_count = transform_asset_tasks(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_asset_tasks", "stg_asset_tasks", run_id, asset_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Asset Tasks: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_assets(db, run_id: str):
    """Transform raw_asset_tasks to stg_assets using SQL aggregation (RPC)"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming assets...")

    # Clear ALL existing staging data (full refresh)
    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_assets')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_assets")

    # Use SQL aggregation via RPC — single call, no 1000-row cap
    print(f"[{datetime.now():%H:%M:%S}] Running SQL aggregation via RPC...")
    assets_list = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.aggregate_assets_from_raw($1)',
        run_id
    )

    print(f"[{datetime.now():%H:%M:%S}] Found {len(assets_list):,} unique assets")

    # Insert in batches via executemany with UPSERT
    batch_size = 5000
    for i in range(0, len(assets_list), batch_size):
        batch = assets_list[i:i + batch_size]
        tuples = [
            (
                dict(row).get("project_did"),
                dict(row).get("asset_did"),
                dict(row).get("asset_id"),
                dict(row).get("asset_name"),
                dict(row).get("task_count"),
                dict(row).get("tasks_pending"),
                dict(row).get("tasks_in_progress"),
                dict(row).get("tasks_submitted"),
                dict(row).get("tasks_approved"),
                dict(row).get("tasks_rejected"),
                dict(row).get("tasks_cancelled"),
                dict(row).get("requirement_count"),
                run_id
            )
            for row in batch
        ]
        db.executemany(
            f'INSERT INTO {SCHEMA_STAGING}.stg_assets '
            f'(project_did, asset_did, asset_id, asset_name, task_count, tasks_pending, '
            f'tasks_in_progress, tasks_submitted, tasks_approved, tasks_rejected, tasks_cancelled, '
            f'requirement_count, run_id) '
            f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) '
            f'ON CONFLICT (project_did, asset_did) DO UPDATE SET '
            f'asset_id=EXCLUDED.asset_id, asset_name=EXCLUDED.asset_name, '
            f'task_count=EXCLUDED.task_count, tasks_pending=EXCLUDED.tasks_pending, '
            f'tasks_in_progress=EXCLUDED.tasks_in_progress, tasks_submitted=EXCLUDED.tasks_submitted, '
            f'tasks_approved=EXCLUDED.tasks_approved, tasks_rejected=EXCLUDED.tasks_rejected, '
            f'tasks_cancelled=EXCLUDED.tasks_cancelled, '
            f'requirement_count=EXCLUDED.requirement_count, run_id=EXCLUDED.run_id',
            tuples
        )

    print(f"[{datetime.now():%H:%M:%S}] Inserted {len(assets_list):,} assets")
    return len(assets_list)


def run_assets_transform(run_id: str = None, client=None):
    """Run assets transformation only"""
    print(f"\n{'='*60}")
    print(f"Assets Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks run_id: {run_id}")
        else:
            print("No successful asset_tasks pipeline runs found")
            return

    run_id = str(run_id)
    asset_count = transform_assets(db, run_id)

    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_assets')
    print(f"\nRow Count Validation:")
    print(f"  [stg_assets]: transformed={asset_count:,} | staging={stg_count:,} (aggregated from raw_asset_tasks)")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Assets: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_asset_tasks(db, run_id: str):
    """Transform raw_asset_tasks to stg_asset_tasks using server-side SQL.

    Runs entirely in PostgreSQL — no data transfer to Python.
    Processes 2.2M rows in ~2-3 minutes vs ~44 minutes with Python round-trips.
    """
    print(f"[{datetime.now():%H:%M:%S}] Transforming asset tasks...")

    # Clear ALL existing staging data (full refresh)
    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_asset_tasks')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_asset_tasks")

    # SQL helper: parse epoch-ms or ISO date string to date (Eastern Time)
    # Matches Python's parse_task_date() logic
    def _date_expr(field):
        return (
            f"CASE "
            f"WHEN r.data->>'{field}' ~ '^[0-9]+$' "
            f"  AND (r.data->>'{field}')::bigint > 9999999999 "
            f"  THEN (TO_TIMESTAMP((r.data->>'{field}')::bigint / 1000.0) "
            f"        AT TIME ZONE 'America/New_York')::date "
            f"WHEN r.data->>'{field}' ~ '^[0-9]+$' "
            f"  THEN (TO_TIMESTAMP((r.data->>'{field}')::bigint) "
            f"        AT TIME ZONE 'America/New_York')::date "
            f"WHEN r.data->>'{field}' IS NOT NULL AND r.data->>'{field}' != '' "
            f"  THEN LEFT(r.data->>'{field}', 10)::date "
            f"ELSE NULL END"
        )

    # SQL: clean_task_name — strip prefix "1. 2a. " and suffix " 123"
    # Matches Python's TASK_NAME_PREFIX_PATTERN and TASK_NAME_SUFFIX_PATTERN
    clean_expr = (
        "TRIM(REGEXP_REPLACE("
        "  REGEXP_REPLACE(r.data->>'Task_Name', '^([0-9]+[a-zA-Z]?\\. *)+', ''), "
        "  '\\s+[0-9]+$', ''))"
    )

    sql = (
        f"INSERT INTO {SCHEMA_STAGING}.stg_asset_tasks "
        f"(project_did, project_status, asset_did, task_did, asset_id, asset_name, "
        f"asset_requirement_count, task_name, task_name_clean, task_status, task_scheduled, "
        f"task_assigned_to_did, task_assigned_to_collection, task_assigned_to_name, "
        f"task_assigned_to_email, task_submitted_on, task_submitted_by_did, "
        f"task_submitted_by_name, task_submitted_by_email, task_approved_on, "
        f"task_approved_by_did, task_approved_by_name, task_approved_by_email, "
        f"task_cancelled_on, task_cancelled_by_did, task_cancelled_by_name, "
        f"task_cancelled_by_email, run_id) "
        f"SELECT "
        f"  r.project_did, "
        f"  r.data->>'Project_Status', "
        f"  r.data->>'Asset_DID', "
        f"  r.data->>'Task_DID', "
        f"  r.data->>'Asset_ID', "
        f"  r.data->>'Asset_Name', "
        f"  (r.data->>'Asset_Requirement_Count')::int, "
        f"  r.data->>'Task_Name', "
        f"  {clean_expr}, "
        f"  r.data->>'Task_Status', "
        f"  {_date_expr('Task_Scheduled')}, "
        f"  r.data->>'Task_Assigned_To_DID', "
        f"  r.data->>'Task_Assigned_To_Collection', "
        f"  r.data->>'Task_Assigned_To_Name', "
        f"  r.data->>'Task_Assigned_To_Email', "
        f"  {_date_expr('Task_Submitted_On')}, "
        f"  r.data->>'Task_Submitted_By_DID', "
        f"  r.data->>'Task_Submitted_By_Name', "
        f"  r.data->>'Task_Submitted_By_Email', "
        f"  {_date_expr('Task_Approved_On')}, "
        f"  r.data->>'Task_Approved_By_DID', "
        f"  r.data->>'Task_Approved_By_Name', "
        f"  r.data->>'Task_Approved_By_Email', "
        f"  {_date_expr('Task_Cancelled_On')}, "
        f"  r.data->>'Task_Cancelled_By_DID', "
        f"  r.data->>'Task_Cancelled_By_Name', "
        f"  r.data->>'Task_Cancelled_By_Email', "
        f"  $1::uuid "
        f"FROM {SCHEMA_RAW}.raw_asset_tasks r "
        f"WHERE r.run_id = $1"
    )

    print(f"[{datetime.now():%H:%M:%S}] Running server-side SQL transform...")
    result = db.execute(sql, run_id)
    # result is like "INSERT 0 2233001"
    total = int(result.split()[-1]) if result else 0

    print(f"[{datetime.now():%H:%M:%S}] Total asset tasks transformed: {total:,}")
    return total


def extract_project_number(project_name: str) -> int:
    """Extract project number from project name like 'TECH-OPS: TS13'"""
    if not project_name:
        return None
    match = re.search(r'TS(\d+)', project_name)
    return int(match.group(1)) if match else None


def transform_qa_forms(db, run_id: str):
    """Transform raw QA form tables to stg_qa_form"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming QA forms...")

    # Clear ALL existing staging data (full refresh)
    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_qa_form')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_qa_form")

    total_transformed = 0

    for form_name, form_config in QA_FORMS.items():
        table_name = form_config["table_name"]
        form_id = form_config["form_id"]
        display_name = form_config["display_name"]

        print(f"[{datetime.now():%H:%M:%S}] Processing {display_name}...")

        result = db.fetch(
            f'SELECT * FROM {SCHEMA_RAW}.{table_name} WHERE run_id = $1',
            run_id
        )

        if not result:
            print(f"[{datetime.now():%H:%M:%S}] {display_name}: 0 rows")
            continue

        rows = []
        for record in result:
            data = record["data"]
            project = data.get("Project", "")
            project_number = extract_project_number(project)

            def get_val(*keys):
                for k in keys:
                    v = data.get(k)
                    if v:
                        return v
                return data.get(keys[0])

            task = data.get("Task")
            rows.append((
                form_name, form_id,
                project, project_number,
                data.get("Site Name"), data.get("Site ID"),
                task, clean_task_name(task),
                data.get("Requirement"), data.get("Requirement Status"),
                data.get("Live Review Performed"), data.get("Swift Used for Photos"),
                data.get("Crew Lead"),
                data.get("Construction Manager (CM)"),
                data.get("Subcontractor (if applicable)"),
                data.get("AAT"), data.get("AAT Issues"), data.get("AAT (Other issues)"),
                data.get("RET"), data.get("RET Issues"), data.get("RET (Others issues)"),
                data.get("RET Values"), data.get("RET Visibility"),
                data.get("Sweeps"), data.get("Sweeps Issues"), data.get("Sweeps (Other issues)"),
                data.get("PIM"), data.get("PIM Issues"), data.get("PIM (Other issues)"),
                data.get("Fiber"), data.get("Fiber Issues"), data.get("Fiber (Other issues)"),
                data.get("Pictures"), data.get("Pictures Issues"), data.get("Pictures (Other issues)"),
                data.get("Sector Photos"), data.get("Powershift Photos"),
                data.get("As-Builts"), data.get("As-Builts Issues"),
                get_val("As-Builts (Other issues)", "AS-Builts (Other issues)"),
                data.get("RF Mitigation"), data.get("RF Mitigation Issues"),
                data.get("RF Mitigation (Other issues)"),
                data.get("Landlord / Tower Owner"), data.get("Landlord / Tower Owner Issues"),
                data.get("Other Landlord-related photos"),
                data.get("Permits"),
                data.get("Additional Documents (if applicable)"),
                data.get("PMI (if applicable)"),
                data.get("(PMI) Vendor Antenna Mount Structural Company"),
                get_val("Others (PMI Vendor):", "Others (PMI Vendor)"),
                data.get("(PMI) Mount Modification Required?"),
                data.get("PMI Issues"), data.get("PMI (Other issues)"),
                data.get("(PMI) Post Modification Inspection Report received?"),
                data.get("Signed PMI Report"),
                data.get("Material Packing List, Signed PMI Report"),
                data.get("Power Testing (if applicable)"),
                data.get("Power Testing Issues"),
                get_val("Power Testing (Other Issues)", "Power Testing (Other issues)"),
                data.get("Connectivity Testing (if applicable)"),
                data.get("Connectivity Testing Issues"),
                data.get("Connectivity Testing (Other Issues)"),
                data.get("Optical Power Testing (if applicable)"),
                get_val("Optical Power Testing (Other Issues)", "Optical Power Testing (Other issues)"),
                data.get("Restoration (if applicable)"),
                data.get("NA Checklist (if applicable)"),
                data.get("N/A Checklist Issues"),
                data.get("N/A Checklist (Other Issues)"),
                data.get("RCM approval"),
                get_val("Completeness of files", "Completeness of Files"),
                data.get("Serials"),
                data.get("Font Size of Labels"),
                data.get("Labels (P-touch, Marks, Tags), Sector Photos, Tape Drop"),
                data.get("Smart Level (Plumb and MDT)"),
                data.get("Calibration Details"),
                data.get("General Ground"),
                data.get("Conditional Pass"),
                data.get("Supports (i.e. Snap-In, etc.)"),
                run_id
            ))

        # Insert in batches
        form_count = len(rows)
        batch_size = 5000
        for i in range(0, form_count, batch_size):
            batch = rows[i:i + batch_size]
            db.executemany(
                f'INSERT INTO {SCHEMA_STAGING}.stg_qa_form '
                f'(form_name, form_id, project, project_number, site_name, site_id, '
                f'task, task_clean, requirement, requirement_status, '
                f'live_review_performed, swift_used_for_photos, crew_lead, '
                f'construction_manager, subcontractor, '
                f'aat, aat_issues, aat_other_issues, '
                f'ret, ret_issues, ret_other_issues, ret_values, ret_visibility, '
                f'sweeps, sweeps_issues, sweeps_other_issues, '
                f'pim, pim_issues, pim_other_issues, '
                f'fiber, fiber_issues, fiber_other_issues, '
                f'pictures, pictures_issues, pictures_other_issues, '
                f'sector_photos, powershift_photos, '
                f'as_builts, as_builts_issues, as_builts_other_issues, '
                f'rf_mitigation, rf_mitigation_issues, rf_mitigation_other_issues, '
                f'landlord_tower_owner, landlord_tower_owner_issues, other_landlord_photos, '
                f'permits, additional_documents, '
                f'pmi, pmi_vendor, pmi_others_vendor, pmi_mount_modification_required, '
                f'pmi_issues, pmi_other_issues, pmi_report_received, '
                f'signed_pmi_report, material_packing_signed_pmi, '
                f'power_testing, power_testing_issues, power_testing_other_issues, '
                f'connectivity_testing, connectivity_testing_issues, connectivity_testing_other_issues, '
                f'optical_power_testing, optical_power_testing_other_issues, '
                f'restoration, na_checklist, na_checklist_issues, na_checklist_other_issues, '
                f'rcm_approval, completeness_of_files, serials, font_size_of_labels, '
                f'labels_sector_tape, smart_level, calibration_details, '
                f'general_ground, conditional_pass, supports, run_id) '
                f'VALUES ({",".join(f"${i}" for i in range(1, 81))})',
                batch
            )

        total_transformed += form_count
        print(f"[{datetime.now():%H:%M:%S}] {display_name}: {form_count:,} rows")

    print(f"[{datetime.now():%H:%M:%S}] Total QA forms transformed: {total_transformed:,}")
    return total_transformed


def run_qa_forms_transform(run_id: str = None, client=None):
    """Run QA forms transformation only"""
    print(f"\n{'='*60}")
    print(f"QA Forms Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "forms_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest forms run_id: {run_id}")
        else:
            print("No successful forms pipeline runs found")
            return

    qa_count = transform_qa_forms(db, run_id)

    raw_tables = [cfg["table_name"] for cfg in QA_FORMS.values()]
    print(f"\nRow Count Validation:")
    validate_transform_counts(db, raw_tables, "stg_qa_form", run_id, qa_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  QA Forms: {qa_count:,}")
    print(f"{'='*60}\n")


def transform_timer_activities(db, run_id: str):
    """Transform raw_timer_activities to stg_timer_activities (append mode - preserves all runs)"""
    print(f"[{datetime.now():%H:%M:%S}] Transforming timer activities...")

    # Get run metadata
    meta = db.fetchrow(
        f'SELECT run_date, start_date, end_date FROM {SCHEMA_RAW}.raw_timer_activities '
        f'WHERE run_id = $1 LIMIT 1',
        run_id
    )
    if not meta:
        print(f"[{datetime.now():%H:%M:%S}] No timer data found for run_id: {run_id}")
        return 0

    run_date = parse_date(meta["run_date"])
    start_date = parse_date(meta["start_date"])
    end_date = parse_date(meta["end_date"])

    print(f"[{datetime.now():%H:%M:%S}] Run date: {run_date}, Date range: {start_date} to {end_date}")

    # Delete existing staging data for this run_id only
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_timer_activities WHERE run_id = $1',
        run_id
    )

    # Fetch all raw data
    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_timer_activities WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No timer activities to transform")
        return 0

    rows = []
    for record in result:
        data = record["data"]
        project = data.get("Project", "")
        project_number = extract_project_number(project)
        task = data.get("Task")

        rows.append((
            project, project_number, record["project_did"],
            data.get("Site Name"), data.get("Site ID"),
            task, clean_task_name(task),
            data.get("Site Lat"), data.get("Site Long"),
            data.get("User Lat"), data.get("User Long"),
            data.get("User Accuracy (m)"), data.get("Site vs User (km)"),
            parse_timestamp(data.get("Start Time")), parse_timestamp(data.get("End Time")),
            data.get("Duration (min)"),
            data.get("User Name"), data.get("User Email"), data.get("User Role"),
            run_id, run_date, start_date, end_date
        ))

    batch_size = 5000
    total = len(rows)
    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        db.executemany(
            f'INSERT INTO {SCHEMA_STAGING}.stg_timer_activities '
            f'(project, project_number, project_did, site_name, site_id, '
            f'task, task_clean, site_lat, site_long, user_lat, user_long, '
            f'user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min, '
            f'user_name, user_email, user_role, run_id, run_date, start_date, end_date) '
            f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)',
            batch
        )

    print(f"[{datetime.now():%H:%M:%S}] Total timer activities transformed: {total:,}")
    return total


def run_timer_transform(run_id: str = None, client=None):
    """Run timer activities transformation only"""
    print(f"\n{'='*60}")
    print(f"Timer Activities Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "timer_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest timer run_id: {run_id}")
        else:
            print("No successful timer pipeline runs found")
            return

    timer_count = transform_timer_activities(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_timer_activities", "stg_timer_activities", run_id, timer_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Timer Activities: {timer_count:,}")
    print(f"{'='*60}\n")


def transform_ar_aging(db, run_id: str):
    """Transform raw_ar_aging to stg_ar_aging for a specific run_id (append mode)."""
    print(f"[{datetime.now():%H:%M:%S}] Transforming AR aging...")

    # Delete existing staging data for this run_id
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_ar_aging WHERE run_id = $1',
        run_id
    )

    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_ar_aging WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No AR aging data to transform")
        return 0

    rows = []
    for record in result:
        data = record["data"]
        rows.append((
            parse_date(record["as_of_date"]),
            parse_timestamp(record.get("email_received_date")),
            data.get("aging_bucket"),
            parse_date(data.get("date")),
            data.get("transaction_type"),
            data.get("num"),
            data.get("customer"),
            data.get("location"),
            parse_date(data.get("due_date")),
            data.get("amount"),
            data.get("open_balance"),
            data.get("past_due"),
            data.get("po_number"),
            run_id,
        ))

    batch_size = 5000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        db.executemany(
            f'INSERT INTO {SCHEMA_STAGING}.stg_ar_aging '
            f'(as_of_date, email_received_date, aging_bucket, date, transaction_type, '
            f'num, customer, location, due_date, amount, open_balance, past_due, po_number, run_id) '
            f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)',
            batch
        )

    total = len(rows)
    print(f"[{datetime.now():%H:%M:%S}] Total AR aging records transformed: {total:,}")
    return total


def run_ar_aging_transform(run_id: str = None, client=None):
    """Run AR aging transformation only."""
    print(f"\n{'='*60}")
    print(f"AR Aging Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "ar_aging_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest ar_aging run_id: {run_id}")
        else:
            print("No successful AR aging pipeline runs found")
            return

    aging_count = transform_ar_aging(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_ar_aging", "stg_ar_aging", run_id, aging_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  AR Aging Records: {aging_count:,}")
    print(f"{'='*60}\n")


def transform_sales_detail(db, run_id: str):
    """Transform raw_sales_detail to stg_sales_detail for a specific run_id (append mode)."""
    print(f"[{datetime.now():%H:%M:%S}] Transforming sales detail...")

    # Delete existing staging data for this run_id
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_sales_detail WHERE run_id = $1',
        run_id
    )

    result = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.raw_sales_detail WHERE run_id = $1',
        run_id
    )

    if not result:
        print(f"[{datetime.now():%H:%M:%S}] No sales detail data to transform")
        return 0

    rows = []
    for record in result:
        data = record["data"]
        rows.append((
            parse_date(record["as_of_date"]),
            parse_timestamp(record.get("email_received_date")),
            parse_date(data.get("date")),
            data.get("transaction_type"),
            data.get("num"),
            data.get("customer"),
            data.get("memo_description"),
            data.get("qty"),
            data.get("sales_price"),
            data.get("amount"),
            data.get("balance"),
            data.get("po_number"),
            parse_date(data.get("service_date")),
            run_id,
        ))

    batch_size = 5000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        db.executemany(
            f'INSERT INTO {SCHEMA_STAGING}.stg_sales_detail '
            f'(as_of_date, email_received_date, date, transaction_type, num, customer, '
            f'memo_description, qty, sales_price, amount, balance, po_number, service_date, run_id) '
            f'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)',
            batch
        )

    total = len(rows)
    print(f"[{datetime.now():%H:%M:%S}] Total sales detail records transformed: {total:,}")
    return total


def run_sales_detail_transform(run_id: str = None, client=None):
    """Run sales detail transformation only."""
    print(f"\n{'='*60}")
    print(f"Sales Detail Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "sales_detail_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest sales_detail run_id: {run_id}")
        else:
            print("No successful sales detail pipeline runs found")
            return

    sales_count = transform_sales_detail(db, run_id)

    print(f"\nRow Count Validation:")
    validate_transform_counts(db, "raw_sales_detail", "stg_sales_detail", run_id, sales_count)

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  Sales Detail Records: {sales_count:,}")
    print(f"{'='*60}\n")


def backfill_asset_did(client=None):
    """Backfill asset_did on stg_timer_activities and stg_qa_form from stg_assets."""
    print(f"\n{'='*60}")
    print(f"Asset DID Backfill")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    # Verify stg_assets has data
    count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_assets')
    if not count:
        print("stg_assets is empty -- skipping asset_did backfill")
        return

    result = db.fetchrow(
        f'SELECT * FROM {SCHEMA_STAGING}.backfill_asset_did()'
    )

    if result:
        timer_updated = result.get("timer_updated", 0)
        qa_form_updated = result.get("qa_form_updated", 0)
        print(f"  Timer rows updated:   {timer_updated:,}")
        print(f"  QA Form rows updated: {qa_form_updated:,}")
    else:
        print("  RPC returned no data")

    print(f"\n{'='*60}\n")


def refresh_analytics(client=None):
    """Refresh analytics materialized views."""
    print(f"\n{'='*60}")
    print(f"Analytics MV Refresh")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    mvs = ["mv_project_summary", "mv_technician_stats", "mv_daily_completion"]
    for mv in mvs:
        result = db.fetchrow(
            f'SELECT * FROM analytics.refresh_one_mv($1)',
            mv
        )
        if result:
            print(f"  {result['view_name']}: {result['refresh_time_ms']:,}ms")
        else:
            print(f"  {mv}: no data returned")

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
        elif sys.argv[1] == "ar_aging":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_ar_aging_transform(run_id)
        elif sys.argv[1] == "sales":
            run_id = sys.argv[2] if len(sys.argv) > 2 else None
            run_sales_detail_transform(run_id)
        else:
            print(f"Unknown transform type: {sys.argv[1]}")
            print("Usage: python transform.py [assets|asset_tasks|qa_forms|timer|ar_aging|sales] [run_id]")
    else:
        run_transform()
