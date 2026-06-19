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
    get_db, SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_REFERENCE, SCHEMA_PIPELINE,
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


def run_orgs_projects_transform(run_id: str = None):
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


def run_user_priorities_transform(run_id: str = None):
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


def run_asset_tasks_transform(run_id: str = None):
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

    # Use SQL aggregation via RPC — single call, no 1000-row cap.
    # 600s timeout: aggregates 2.2M raw rows, can take >5 min under load.
    print(f"[{datetime.now():%H:%M:%S}] Running SQL aggregation via RPC...")
    assets_list = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.aggregate_assets_from_raw($1)',
        run_id,
        statement_timeout=600,
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


def enrich_stg_assets_with_status():
    """Populate stg_assets.asset_status from data_raw.raw_assets.

    Called after extract_assets loads raw_assets. Idempotent -- only updates
    rows whose status actually changed. Returns rows updated count.
    """
    db = get_db()
    print(f"[{datetime.now():%H:%M:%S}] Enriching stg_assets.asset_status from raw_assets...")

    updated = db.fetchval(
        f'''
        WITH upd AS (
            UPDATE {SCHEMA_STAGING}.stg_assets s
            SET asset_status = r.asset_status
            FROM {SCHEMA_RAW}.raw_assets r
            WHERE s.project_did = r.project_did
              AND s.asset_did = r.asset_did
              AND s.asset_status IS DISTINCT FROM r.asset_status
            RETURNING 1
        )
        SELECT COUNT(*) FROM upd
        '''
    )
    print(f"[{datetime.now():%H:%M:%S}] Enriched {updated:,} stg_assets rows with asset_status")
    return updated


def run_assets_transform(run_id: str = None):
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

    # NOTE: the full-refresh clear is NOT a separate DELETE anymore. It is
    # folded into the INSERT below as a data-modifying CTE so the clear and
    # the reload run as ONE atomic statement. Previously the DELETE was its
    # own auto-committed call; when the big INSERT hit statement_timeout and
    # rolled back, the table was left EMPTY until the next run (2026-06-05
    # incident). A single statement can never leave stg_asset_tasks empty.

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
        # Data-modifying CTE: clear the table, then reload — atomically.
        f"WITH cleared AS (DELETE FROM {SCHEMA_STAGING}.stg_asset_tasks RETURNING 1) "
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
    # 900s timeout: stg_asset_tasks reload is now ~2.6M rows and growing; the
    # default 300s tripped on 2026-06-05. Sibling transforms use 600s; the
    # atomic clear+reload here warrants the larger ceiling.
    result = db.execute(sql, run_id, statement_timeout=900)
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
    """Transform raw QA form tables to stg_qa_form using server-side SQL.

    Runs entirely in PostgreSQL — no data transfer to Python.
    Uses UNION ALL across all 6 raw form tables, with JSONB field extraction.
    """
    print(f"[{datetime.now():%H:%M:%S}] Transforming QA forms...")

    # Clear ALL existing staging data (full refresh)
    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_qa_form')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_qa_form")

    # SQL: clean_task_name — strip prefix "1. 2a. " and suffix " 123"
    clean_task = (
        "TRIM(REGEXP_REPLACE("
        "  REGEXP_REPLACE(r.data->>'Task', '^([0-9]+[a-zA-Z]?\\. *)+', ''), "
        "  '\\s+[0-9]+$', ''))"
    )

    # SQL helper: COALESCE for fields with alternate key names (replaces Python get_val)
    def coalesce_field(key1, key2):
        return f"COALESCE(NULLIF(r.data->>'{key1}', ''), r.data->>'{key2}')"

    # Build UNION ALL across all form tables
    union_parts = []
    for form_name, form_config in QA_FORMS.items():
        table_name = form_config["table_name"]
        form_id = form_config["form_id"]

        part = (
            f"SELECT "
            f"  '{form_name}' AS form_name, "
            f"  '{form_id}' AS form_id, "
            f"  r.data->>'Project' AS project, "
            f"  (REGEXP_MATCH(r.data->>'Project', 'TS(\\d+)'))[1]::int AS project_number, "
            f"  r.data->>'Site Name' AS site_name, "
            f"  r.data->>'Site ID' AS site_id, "
            f"  r.data->>'Task' AS task, "
            f"  {clean_task} AS task_clean, "
            f"  r.data->>'Requirement' AS requirement, "
            f"  r.data->>'Requirement Status' AS requirement_status, "
            f"  r.data->>'Live Review Performed' AS live_review_performed, "
            f"  r.data->>'Swift Used for Photos' AS swift_used_for_photos, "
            f"  r.data->>'Crew Lead' AS crew_lead, "
            f"  r.data->>'Construction Manager (CM)' AS construction_manager, "
            f"  r.data->>'Subcontractor (if applicable)' AS subcontractor, "
            f"  r.data->>'AAT' AS aat, "
            f"  r.data->>'AAT Issues' AS aat_issues, "
            f"  r.data->>'AAT (Other issues)' AS aat_other_issues, "
            f"  r.data->>'RET' AS ret, "
            f"  r.data->>'RET Issues' AS ret_issues, "
            f"  r.data->>'RET (Others issues)' AS ret_other_issues, "
            f"  r.data->>'RET Values' AS ret_values, "
            f"  r.data->>'RET Visibility' AS ret_visibility, "
            f"  r.data->>'Sweeps' AS sweeps, "
            f"  r.data->>'Sweeps Issues' AS sweeps_issues, "
            f"  r.data->>'Sweeps (Other issues)' AS sweeps_other_issues, "
            f"  r.data->>'PIM' AS pim, "
            f"  r.data->>'PIM Issues' AS pim_issues, "
            f"  r.data->>'PIM (Other issues)' AS pim_other_issues, "
            f"  r.data->>'Fiber' AS fiber, "
            f"  r.data->>'Fiber Issues' AS fiber_issues, "
            f"  r.data->>'Fiber (Other issues)' AS fiber_other_issues, "
            f"  r.data->>'Pictures' AS pictures, "
            f"  r.data->>'Pictures Issues' AS pictures_issues, "
            f"  r.data->>'Pictures (Other issues)' AS pictures_other_issues, "
            f"  r.data->>'Sector Photos' AS sector_photos, "
            f"  r.data->>'Powershift Photos' AS powershift_photos, "
            f"  r.data->>'As-Builts' AS as_builts, "
            f"  r.data->>'As-Builts Issues' AS as_builts_issues, "
            f"  {coalesce_field('As-Builts (Other issues)', 'AS-Builts (Other issues)')} AS as_builts_other_issues, "
            f"  r.data->>'RF Mitigation' AS rf_mitigation, "
            f"  r.data->>'RF Mitigation Issues' AS rf_mitigation_issues, "
            f"  r.data->>'RF Mitigation (Other issues)' AS rf_mitigation_other_issues, "
            f"  r.data->>'Landlord / Tower Owner' AS landlord_tower_owner, "
            f"  r.data->>'Landlord / Tower Owner Issues' AS landlord_tower_owner_issues, "
            f"  r.data->>'Other Landlord-related photos' AS other_landlord_photos, "
            f"  r.data->>'Permits' AS permits, "
            f"  r.data->>'Additional Documents (if applicable)' AS additional_documents, "
            f"  r.data->>'PMI (if applicable)' AS pmi, "
            f"  r.data->>'(PMI) Vendor Antenna Mount Structural Company' AS pmi_vendor, "
            f"  {coalesce_field('Others (PMI Vendor):', 'Others (PMI Vendor)')} AS pmi_others_vendor, "
            f"  r.data->>'(PMI) Mount Modification Required?' AS pmi_mount_modification_required, "
            f"  r.data->>'PMI Issues' AS pmi_issues, "
            f"  r.data->>'PMI (Other issues)' AS pmi_other_issues, "
            f"  r.data->>'(PMI) Post Modification Inspection Report received?' AS pmi_report_received, "
            f"  r.data->>'Signed PMI Report' AS signed_pmi_report, "
            f"  r.data->>'Material Packing List, Signed PMI Report' AS material_packing_signed_pmi, "
            f"  r.data->>'Power Testing (if applicable)' AS power_testing, "
            f"  r.data->>'Power Testing Issues' AS power_testing_issues, "
            f"  {coalesce_field('Power Testing (Other Issues)', 'Power Testing (Other issues)')} AS power_testing_other_issues, "
            f"  r.data->>'Connectivity Testing (if applicable)' AS connectivity_testing, "
            f"  r.data->>'Connectivity Testing Issues' AS connectivity_testing_issues, "
            f"  r.data->>'Connectivity Testing (Other Issues)' AS connectivity_testing_other_issues, "
            f"  r.data->>'Optical Power Testing (if applicable)' AS optical_power_testing, "
            f"  {coalesce_field('Optical Power Testing (Other Issues)', 'Optical Power Testing (Other issues)')} AS optical_power_testing_other_issues, "
            f"  r.data->>'Restoration (if applicable)' AS restoration, "
            f"  r.data->>'NA Checklist (if applicable)' AS na_checklist, "
            f"  r.data->>'N/A Checklist Issues' AS na_checklist_issues, "
            f"  r.data->>'N/A Checklist (Other Issues)' AS na_checklist_other_issues, "
            f"  r.data->>'RCM approval' AS rcm_approval, "
            f"  {coalesce_field('Completeness of files', 'Completeness of Files')} AS completeness_of_files, "
            f"  r.data->>'Serials' AS serials, "
            f"  r.data->>'Font Size of Labels' AS font_size_of_labels, "
            f"  r.data->>'Labels (P-touch, Marks, Tags), Sector Photos, Tape Drop' AS labels_sector_tape, "
            f"  r.data->>'Smart Level (Plumb and MDT)' AS smart_level, "
            f"  r.data->>'Calibration Details' AS calibration_details, "
            f"  r.data->>'General Ground' AS general_ground, "
            f"  r.data->>'Conditional Pass' AS conditional_pass, "
            f"  r.data->>'Supports (i.e. Snap-In, etc.)' AS supports, "
            f"  $1::uuid AS run_id "
            f"FROM {SCHEMA_RAW}.{table_name} r "
            f"WHERE r.run_id = $1"
        )
        union_parts.append(part)

    union_sql = " UNION ALL ".join(union_parts)

    sql = (
        f"INSERT INTO {SCHEMA_STAGING}.stg_qa_form "
        f"(form_name, form_id, project, project_number, site_name, site_id, "
        f"task, task_clean, requirement, requirement_status, "
        f"live_review_performed, swift_used_for_photos, crew_lead, "
        f"construction_manager, subcontractor, "
        f"aat, aat_issues, aat_other_issues, "
        f"ret, ret_issues, ret_other_issues, ret_values, ret_visibility, "
        f"sweeps, sweeps_issues, sweeps_other_issues, "
        f"pim, pim_issues, pim_other_issues, "
        f"fiber, fiber_issues, fiber_other_issues, "
        f"pictures, pictures_issues, pictures_other_issues, "
        f"sector_photos, powershift_photos, "
        f"as_builts, as_builts_issues, as_builts_other_issues, "
        f"rf_mitigation, rf_mitigation_issues, rf_mitigation_other_issues, "
        f"landlord_tower_owner, landlord_tower_owner_issues, other_landlord_photos, "
        f"permits, additional_documents, "
        f"pmi, pmi_vendor, pmi_others_vendor, pmi_mount_modification_required, "
        f"pmi_issues, pmi_other_issues, pmi_report_received, "
        f"signed_pmi_report, material_packing_signed_pmi, "
        f"power_testing, power_testing_issues, power_testing_other_issues, "
        f"connectivity_testing, connectivity_testing_issues, connectivity_testing_other_issues, "
        f"optical_power_testing, optical_power_testing_other_issues, "
        f"restoration, na_checklist, na_checklist_issues, na_checklist_other_issues, "
        f"rcm_approval, completeness_of_files, serials, font_size_of_labels, "
        f"labels_sector_tape, smart_level, calibration_details, "
        f"general_ground, conditional_pass, supports, run_id) "
        f"{union_sql}"
    )

    print(f"[{datetime.now():%H:%M:%S}] Running server-side SQL transform...")
    result = db.execute(sql, run_id)
    total = int(result.split()[-1]) if result else 0

    print(f"[{datetime.now():%H:%M:%S}] Total QA forms transformed: {total:,}")
    return total


def run_qa_forms_transform(run_id: str = None):
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

    # Delete ALL staging data for the same extraction month (start_date).
    # Each nightly run extracts the full month-to-date, so the latest run
    # is always a superset. Scoping by start_date prevents stacking across runs.
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_timer_activities WHERE start_date = $1',
        start_date
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
        start_time = parse_timestamp(data.get("Start Time"))

        rows.append((
            project, project_number, record["project_did"],
            data.get("Site Name"), data.get("Site ID"),
            task, clean_task_name(task),
            data.get("Site Lat"), data.get("Site Long"),
            data.get("User Lat"), data.get("User Long"),
            data.get("User Accuracy (m)"), data.get("Site vs User (km)"),
            start_time, parse_timestamp(data.get("End Time")),
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


def run_timer_transform(run_id: str = None):
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

    # Delete staging rows for any email dates covered by this run.
    # Scoping by email_received_date (not run_id) prevents stacking when
    # the same email is re-processed across different pipeline runs.
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_ar_aging WHERE email_received_date IN '
        f'(SELECT DISTINCT email_received_date FROM {SCHEMA_RAW}.raw_ar_aging WHERE run_id = $1)',
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


def run_ar_aging_transform(run_id: str = None):
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

    # Delete staging rows for any email dates covered by this run.
    # Scoping by email_received_date (not run_id) prevents stacking when
    # the same email is re-processed across different pipeline runs.
    db.execute(
        f'DELETE FROM {SCHEMA_STAGING}.stg_sales_detail WHERE email_received_date IN '
        f'(SELECT DISTINCT email_received_date FROM {SCHEMA_RAW}.raw_sales_detail WHERE run_id = $1)',
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


def run_sales_detail_transform(run_id: str = None):
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


def backfill_asset_did():
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

    # Backfill carrier_group on stg_assets from reference.ref_carrier_groups
    # (moved from data_staging.carrier_group_lookup in OntelDB reorg Phase A, migration 113)
    updated = db.fetchval(f"""
        WITH matched AS (
            SELECT DISTINCT ON (a.asset_did)
                a.asset_did, cg.carrier_group
            FROM {SCHEMA_STAGING}.stg_assets a
            JOIN {SCHEMA_REFERENCE}.ref_carrier_groups cg
                ON a.asset_id ILIKE '%' || cg.search_term || '%'
            WHERE a.carrier_group IS NULL
            ORDER BY a.asset_did, cg.match_order
        ),
        do_update AS (
            UPDATE {SCHEMA_STAGING}.stg_assets a
            SET carrier_group = m.carrier_group
            FROM matched m
            WHERE a.asset_did = m.asset_did
            RETURNING 1
        )
        SELECT COUNT(*) FROM do_update
    """)
    updated = updated or 0
    print(f"  Carrier group backfill: {updated:,} assets updated")

    print(f"\n{'='*60}\n")


def refresh_analytics():
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


def refresh_quote_mvs():
    """Refresh the Quote Automation materialized views.

    mv_quote_invoice_options + mv_quote_review feed the quote-automation app
    (which reads analytics.v_quote_review = mv_quote_review LEFT JOIN the
    overrides table). Both derive from stg_asset_tasks (worklist) and
    stg_invoicing_form (priced lines), so they go stale whenever either source
    reloads. Refreshed CONCURRENTLY via analytics.refresh_one_mv (migration 085).

    Independent of refresh_analytics()'s three core MVs. Failures here are logged
    but never raised, so a quote-side issue can't block the nightly pipeline or
    its downstream dispatches.
    """
    print(f"\n{'='*60}")
    print(f"Quote MV Refresh")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    # Options first (the app's chosen-line picker reads it); review second; the
    # Data Source tab's source-invoice-lines MV last.
    mvs = ["mv_quote_invoice_options", "mv_quote_review", "mv_quote_source_invoice_lines"]
    for mv in mvs:
        try:
            result = db.fetchrow(
                'SELECT * FROM analytics.refresh_one_mv($1)',
                mv
            )
            if result:
                print(f"  {result['view_name']}: {result['refresh_time_ms']:,}ms")
            else:
                print(f"  {mv}: no data returned")
        except Exception as e:
            print(f"  {mv}: FAILED (non-fatal) — {e}")

    print(f"\n{'='*60}\n")


# =============================================================================
# GC PIPELINE TRANSFORMS
# Clones of the Ontel transforms above with these substitutions:
#   raw_asset_tasks       -> raw_asset_tasks_gc
#   stg_asset_tasks       -> stg_asset_tasks_gc
#   stg_assets            -> stg_assets_gc
#   aggregate_assets_from_raw -> aggregate_assets_gc
# Logic is otherwise identical. See spec
#   docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md
# =============================================================================


def transform_assets_gc(db, run_id: str):
    """GC assets transform — clone of transform_assets reading raw_asset_tasks_gc
    and writing stg_assets_gc via the aggregate_assets_gc RPC.
    """
    print(f"[{datetime.now():%H:%M:%S}] Transforming GC assets...")

    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_assets_gc')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_assets_gc")

    print(f"[{datetime.now():%H:%M:%S}] Running SQL aggregation via aggregate_assets_gc...")
    assets_list = db.fetch(
        f'SELECT * FROM {SCHEMA_RAW}.aggregate_assets_gc($1)',
        run_id,
        statement_timeout=600,
    )

    print(f"[{datetime.now():%H:%M:%S}] Found {len(assets_list):,} unique GC assets")

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
            f'INSERT INTO {SCHEMA_STAGING}.stg_assets_gc '
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

    print(f"[{datetime.now():%H:%M:%S}] Inserted {len(assets_list):,} GC assets")
    return len(assets_list)


def run_assets_gc_transform(run_id: str = None):
    """Run GC assets transformation only.

    Aggregates from raw_asset_tasks_gc into stg_assets_gc via the
    aggregate_assets_gc RPC. Mirror of run_assets_transform.
    """
    print(f"\n{'='*60}")
    print(f"GC Assets Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_gc_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks_gc_extract run_id: {run_id}")
        else:
            print("No successful asset_tasks_gc_extract runs found")
            return

    run_id = str(run_id)
    asset_count = transform_assets_gc(db, run_id)

    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_assets_gc')
    print(f"\nRow Count Validation:")
    print(f"  [stg_assets_gc]: transformed={asset_count:,} | staging={stg_count:,}")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  GC Assets: {asset_count:,}")
    print(f"{'='*60}\n")


def transform_asset_tasks_gc(db, run_id: str):
    """GC asset tasks transform — clone of transform_asset_tasks reading
    raw_asset_tasks_gc and writing stg_asset_tasks_gc.

    All JSONB field extraction logic (date parsing, task_name cleanup)
    is identical to the Ontel transform.
    """
    print(f"[{datetime.now():%H:%M:%S}] Transforming GC asset tasks...")

    db.execute(f'DELETE FROM {SCHEMA_STAGING}.stg_asset_tasks_gc')
    print(f"[{datetime.now():%H:%M:%S}] Cleared old data from stg_asset_tasks_gc")

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

    clean_expr = (
        "TRIM(REGEXP_REPLACE("
        "  REGEXP_REPLACE(r.data->>'Task_Name', '^([0-9]+[a-zA-Z]?\\. *)+', ''), "
        "  '\\s+[0-9]+$', ''))"
    )

    sql = (
        f"INSERT INTO {SCHEMA_STAGING}.stg_asset_tasks_gc "
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
        f"FROM {SCHEMA_RAW}.raw_asset_tasks_gc r "
        f"WHERE r.run_id = $1"
    )

    print(f"[{datetime.now():%H:%M:%S}] Running server-side GC SQL transform...")
    result = db.execute(sql, run_id, statement_timeout=600)
    total = int(result.split()[-1]) if result else 0

    print(f"[{datetime.now():%H:%M:%S}] Total GC asset tasks transformed: {total:,}")
    return total


def run_asset_tasks_gc_transform(run_id: str = None):
    """Run GC asset tasks transformation only.

    Reads raw_asset_tasks_gc and writes stg_asset_tasks_gc via Python+SQL
    (matching the Ontel pattern; not a SQL RPC). Mirror of
    run_asset_tasks_transform.
    """
    print(f"\n{'='*60}")
    print(f"GC Asset Tasks Transformation")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    if not run_id:
        row = db.fetchrow(
            f'SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs '
            f'WHERE pipeline_name = $1 AND status = $2 '
            f'ORDER BY started_at DESC LIMIT 1',
            "asset_tasks_gc_extract", "success"
        )
        if row:
            run_id = str(row["run_id"])
            print(f"Using latest asset_tasks_gc_extract run_id: {run_id}")
        else:
            print("No successful asset_tasks_gc_extract runs found")
            return

    asset_count = transform_asset_tasks_gc(db, run_id)

    print(f"\nRow Count Validation:")
    raw_count = db.fetchval(
        f'SELECT COUNT(*) FROM {SCHEMA_RAW}.raw_asset_tasks_gc WHERE run_id = $1',
        run_id
    )
    stg_count = db.fetchval(f'SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_asset_tasks_gc')
    status = "OK" if raw_count == stg_count else "MISMATCH"
    print(f"  [stg_asset_tasks_gc]: raw={raw_count:,} | transformed={asset_count:,} "
          f"| staging={stg_count:,} [{status}]")

    print(f"\n{'='*60}")
    print(f"Transformation Summary:")
    print(f"  GC Asset Tasks: {asset_count:,}")
    print(f"{'='*60}\n")


def refresh_analytics_gc():
    """Refresh the three _gc materialized views.

    Calls analytics.refresh_one_mv() on each. Mirror of refresh_analytics()
    scoped to the _gc set so the Ontel MVs are unaffected.
    """
    print(f"\n{'='*60}")
    print(f"GC Analytics MV Refresh")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'='*60}\n")

    db = get_db()

    mvs = ["mv_project_summary_gc", "mv_technician_stats_gc", "mv_daily_completion_gc"]
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


def transform_invoicing_form(db, run_id: str) -> int:
    """raw_invoicing_form (jsonb) -> stg_invoicing_form (flat + extra_fields)."""
    from config import INVOICING_KNOWN_FIELDS  # noqa: PLC0415

    # jsonb '-' text[] removes known keys, leaving the overflow.
    known_array = "ARRAY[" + ",".join(
        "'" + k.replace("'", "''") + "'" for k in INVOICING_KNOWN_FIELDS
    ) + "]"

    # Atomic clear+reload in ONE statement: a separate DELETE then INSERT (the
    # old shape) can leave stg_invoicing_form EMPTY if the INSERT fails after the
    # DELETE commits (the 2026-06-05 asset-tasks incident). The CTE makes it
    # all-or-nothing.
    sql = f"""
    WITH cleared AS (DELETE FROM {SCHEMA_STAGING}.stg_invoicing_form RETURNING 1)
    INSERT INTO {SCHEMA_STAGING}.stg_invoicing_form
      (form_did, project, site_name, site_id, task, requirement, requirement_status,
       sow, invoice_category, service_rate, ll_cop, landlord, landlord_others,
       pmi_cop, rf_mitigation_cop, fa_number, site_name_norm, extra_fields, run_id)
    SELECT
      r.form_did,
      r.data->>'Project',
      r.data->>'Site Name',
      r.data->>'Site ID',
      r.data->>'Task',
      r.data->>'Requirement',
      r.data->>'Requirement Status',
      r.data->>'Scope of Work (SOW)',
      r.data->>'Invoice Category',
      r.data->>'Service Rate',
      r.data->>'LL COP to be handled by Ontel?',
      r.data->>'Landlord',
      r.data->>'Landlord (Others)',
      r.data->>'PMI COP to be handled by Ontel?',
      COALESCE(r.data->>'RF Mitigation COP to be handled by Ontel?', r.data->>'RF Mitigation COP to be handled by Ontel'),
      (SELECT mm[1]
         FROM regexp_matches(COALESCE(r.data->>'Site ID',''), '(\\d{{6,9}})', 'g') AS mm
        ORDER BY length(mm[1]) DESC, mm[1]
        LIMIT 1),
      NULLIF(UPPER(REGEXP_REPLACE(TRIM(COALESCE(r.data->>'Site Name','')), '\\s+', ' ', 'g')), ''),
      (r.data - {known_array}),
      r.run_id
    FROM {SCHEMA_RAW}.raw_invoicing_form r
    WHERE r.run_id = $1::uuid
    """
    db.execute(sql, run_id)
    return db.fetchval(f"SELECT count(*) FROM {SCHEMA_STAGING}.stg_invoicing_form")


def run_invoicing_transform(run_id: str = None):
    db = get_db()
    if not run_id:
        row = db.fetchrow(
            f"SELECT run_id FROM {SCHEMA_PIPELINE}.pipeline_runs "
            f"WHERE pipeline_name = $1 AND status = $2 ORDER BY started_at DESC LIMIT 1",
            "invoicing_extract", "success",
        )
        run_id = str(row["run_id"]) if row else None
    count = transform_invoicing_form(db, run_id)
    validate_transform_counts(db, ["raw_invoicing_form"], "stg_invoicing_form", run_id, count)
    return count


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
