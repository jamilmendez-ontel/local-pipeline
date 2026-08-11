"""Extract Daily Reports from Swift API → Supabase.

Separate pipeline for management-level data from TECH-OPS: Daily Reports project.
Extracts: Assets (employees) → Tasks (dates) → Requirements (hours) → Timers (clock-in/out)

Usage:
    python extract_daily_reports.py                    # incremental (new tasks only)
    python extract_daily_reports.py --full             # full extract (all tasks)
    python extract_daily_reports.py --days 7           # last 7 days only
"""

import argparse
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from base_extractor import BaseExtractor
from config import (
    SCHEMA_RAW, SCHEMA_STAGING,
    get_logger, get_db, close_db, retry_db, setup_logging,
)

setup_logging()
logger = get_logger("daily_reports")

TZ_EASTERN = ZoneInfo("America/New_York")
MAX_WORKERS = 30
RUN_ID = str(uuid.uuid4())


def discover_projects():
    """Find active Daily Reports projects from Supabase."""
    db = get_db()
    rows = retry_db(
        lambda: db.fetch(
            f"SELECT project_did, project_name, asset_task_count "
            f"FROM {SCHEMA_STAGING}.stg_projects "
            f"WHERE project_name LIKE 'TECH-OPS: Daily Reports%' "
            f"  AND project_name NOT LIKE 'x_Archive%' "
            f"ORDER BY project_name",
        ),
        description="discover projects",
    )
    close_db()
    projects = [(r["project_did"], r["project_name"]) for r in rows]
    for did, name in projects:
        logger.info(f"  {name} ({did})")
    return projects


def parse_work_date(task_name):
    """Parse date from task name (M/D/YYYY or serial number)."""
    if not task_name:
        return None
    task_name = str(task_name).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(task_name, fmt).date()
        except ValueError:
            continue
    # Try serial number
    try:
        serial = int(float(task_name))
        from datetime import timedelta
        return (datetime(1899, 12, 30) + timedelta(days=serial)).date()
    except (ValueError, TypeError, OverflowError):
        return None


def parse_hours(name_field):
    """Parse hours from requirement name field."""
    if not name_field:
        return None
    try:
        return float(str(name_field).strip())
    except ValueError:
        import re
        match = re.search(r"(\d+\.?\d*)", str(name_field))
        return float(match.group(1)) if match else None


def epoch_to_dt(epoch_ms):
    """Convert epoch milliseconds to timezone-aware datetime."""
    if not epoch_ms:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


# Members clock in (start the attendance timer) on a DR task that is still
# 'pending' with no requirement rows -- the timer is the FIRST thing that
# exists on the task. Fetch timers for those while the work date is fresh so
# DR Monitoring sees the clock-in the same day (Jamil Mendez 2026-08-11:
# clock-in 9:08 AM PHT stayed invisible until the task left 'pending' at
# ~4:33 PM PHT). Older pending tasks stay excluded: DR tasks are pre-created
# months ahead (~17.8k pending rows in a 30-day window vs ~140 within this
# lookback, measured 2026-08-11), and a blanket fetch would 8x the API calls.
PENDING_TIMER_LOOKBACK_DAYS = 2


def should_fetch_timers(status, req_count, work_date, today):
    """Timer (attendance) fetch policy for one task. Everything non-pending,
    non-cancelled is fetched; any status with requirement rows is fetched
    (original rule, kept); a 0-req pending task is fetched only while its work
    date is within PENDING_TIMER_LOOKBACK_DAYS of today."""
    if status not in ("pending", "cancelled") or (req_count or 0) > 0:
        return True
    return (
        status == "pending"
        and work_date is not None
        and today - timedelta(days=PENDING_TIMER_LOOKBACK_DAYS) <= work_date <= today
    )


class DailyReportsPipeline:
    def __init__(self):
        self.ext = BaseExtractor(pipeline_name="daily_reports")
        self.ext.authenticate()
        self.headers = {"Authorization": f"Bearer {self.ext.token}"}
        self.base = self.ext.base_url

    def _request(self, url, params=None):
        """Make API request with retry and token refresh."""
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, headers=self.headers, timeout=60)
                if resp.status_code == 401:
                    self.ext.authenticate()
                    self.headers = {"Authorization": f"Bearer {self.ext.token}"}
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                else:
                    raise
        return None

    def fetch_assets(self, project_did):
        """Fetch all assets (employees) for a project."""
        assets = []
        page = 0
        page_size = 1000
        while True:
            data = self._request(
                f"{self.base}/api/projects/{project_did}/assets",
                params={"page": page, "pageSize": page_size},
            )
            rows = data.get("list", [])
            assets.extend(rows)
            if not rows or len(rows) < page_size:
                break
            page += 1
        return assets

    def fetch_tasks(self, asset_project_id):
        """Fetch all tasks (dates) for one employee."""
        tasks = []
        page = 0
        page_size = 1000
        while True:
            data = self._request(
                f"{self.base}/api/asset-projects/{asset_project_id}/asset-tasks",
                params={"page": page, "pageSize": page_size},
            )
            rows = data.get("list", [])
            tasks.extend(rows)
            if not rows or len(rows) < page_size:
                break
            page += 1
        return tasks

    def fetch_requirement_and_timer(self, task_did, timers_only=False, requirements_only=False):
        """Fetch requirements and/or timer for one task.

        Returns (reqs, timers, ok). ok=False means the fetch threw and the
        (empty) lists are NOT authoritative -- Step 5's reconcile-delete must
        skip that task or a transient API failure would wipe its staged rows.
        """
        reqs = []
        timers = []
        ok = True
        try:
            if not timers_only:
                r1 = self._request(f"{self.base}/api/asset-tasks/{task_did}/requirements")
                if r1:
                    reqs = r1.get("list", []) if isinstance(r1, dict) else r1
            if not requirements_only:
                r2 = self._request(f"{self.base}/api/asset-tasks/{task_did}/timer-activities")
                if r2:
                    timers = r2.get("list", []) if isinstance(r2, dict) else r2
        except Exception as e:
            logger.warning(f"Failed req/timer for {task_did}: {e}")
            ok = False
        return reqs, timers, ok

    def run(self, projects, full=False, days=None, timers_only=False, requirements_only=False):
        """Run the full extraction pipeline."""
        db = get_db()

        # Step 1: Fetch assets
        logger.info("=== Step 1: Fetching employees ===")
        all_assets = []
        for project_did, project_name in projects:
            assets = self.fetch_assets(project_did)
            for a in assets:
                emp_id = a.get("shortName", "").rsplit("_", 1)[-1] if "_" in a.get("shortName", "") else a.get("shortName", "")
                all_assets.append({
                    "project_did": project_did,
                    "asset_project_id": a["id"],
                    "asset_did": a["asset"]["id"],
                    "asset_name": a.get("shortName", ""),
                    "emp_id": emp_id,
                    "raw": a,
                })
            logger.info(f"  {project_name}: {len(assets)} employees")

        # Step 2: Fetch tasks for each employee
        logger.info(f"\n=== Step 2: Fetching tasks for {len(all_assets)} employees ===")
        all_tasks = []
        done = 0

        # Staged tasks older than the rolling window that never reached a
        # terminal status: an approval that lands >window days after the work
        # date (bi-monthly approvers batch-approve late) would otherwise never
        # be re-fetched and the row stays 'submitted' forever. The employee
        # task lists are fetched in full regardless, so refreshing these rows
        # costs no extra API calls: they join the Step 3 status upsert as
        # status_only and are excluded from the child fetches in Steps 4-5.
        stale_status_dids = set()
        if not full and days:
            stale_rows = retry_db(
                lambda: db.fetch(
                    f"SELECT task_did FROM {SCHEMA_STAGING}.stg_daily_reports "
                    f"WHERE task_status NOT IN ('approved', 'cancelled') "
                    f"  AND work_date < $1",
                    date.today() - timedelta(days=days),
                ),
                description="stale non-terminal task dids",
            )
            stale_status_dids = {r["task_did"] for r in stale_rows}
            logger.info(f"  Out-of-window non-terminal tasks to status-refresh: {len(stale_status_dids)}")

        def process_employee(asset_info):
            tasks = self.fetch_tasks(asset_info["asset_project_id"])
            parsed = []
            today = date.today()
            for t in tasks:
                if t.get("collection") == "milestones":
                    continue
                work_date = parse_work_date(t.get("name"))
                # Skip future dates — they're empty placeholders
                if work_date and work_date > today:
                    continue
                # Filter by date range for daily mode; out-of-window tasks
                # still awaiting a terminal status stay in for a status-only
                # refresh.
                status_only = False
                if not full and days and work_date:
                    cutoff = today - timedelta(days=days)
                    if work_date < cutoff:
                        if t.get("id") not in stale_status_dids:
                            continue
                        status_only = True
                parsed.append({
                    "asset_info": asset_info,
                    "task": t,
                    "work_date": work_date,
                    "status_only": status_only,
                })
            return parsed

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_employee, a): a for a in all_assets}
            for future in as_completed(futures, timeout=3600):
                tasks = future.result()
                all_tasks.extend(tasks)
                done += 1
                if done % 10 == 0:
                    logger.info(f"  {done}/{len(all_assets)} employees, {len(all_tasks)} tasks")

        logger.info(f"  Total: {len(all_tasks)} tasks")

        # Split tasks for Step 4 (status_only tasks are status-refresh only —
        # no child fetches, no Step 5 reconcile):
        # - Requirements: only fetch for tasks with req_count > 0
        # - Timers: all non-pending/cancelled tasks, plus recent pending ones
        #   (clock-in exists on a pending DR before anything else does) --
        #   see should_fetch_timers()
        tasks_with_reqs = [t for t in all_tasks
                           if not t.get("status_only")
                           and t["task"].get("metrics", {}).get("reqCount", 0) > 0]
        tasks_for_timers = [t for t in all_tasks
                           if not t.get("status_only")
                           and should_fetch_timers(t["task"].get("status"),
                                                   t["task"].get("metrics", {}).get("reqCount", 0),
                                                   t["work_date"], date.today())]
        logger.info(f"  With requirements: {len(tasks_with_reqs)}")
        logger.info(f"  For timer fetch: {len(tasks_for_timers)}")

        # Step 3: Load tasks to raw + staging (batch)
        logger.info(f"\n=== Step 3: Loading {len(all_tasks)} tasks to Supabase ===")

        # Prepare batch data
        raw_batch = []
        stg_batch = []
        for t in all_tasks:
            ai = t["asset_info"]
            task = t["task"]
            wd = t["work_date"]
            tdid = task.get("id", "")

            raw_batch.append((
                "task", tdid, ai["project_did"], ai["asset_did"], tdid,
                json.dumps(task, default=str), RUN_ID, date.today(),
            ))

            submitted_by = task.get("submittedBy", {}).get("name") if isinstance(task.get("submittedBy"), dict) else None
            submitted_on = epoch_to_dt(task.get("submittedOn"))
            approved_by = task.get("approvedBy", {}).get("name") if isinstance(task.get("approvedBy"), dict) else None
            approved_on = epoch_to_dt(task.get("approvedOn"))
            milestone_name = task.get("milestone", {}).get("name") if isinstance(task.get("milestone"), dict) else None
            assigned_approver = task.get("assignedTo", {}).get("name") if isinstance(task.get("assignedTo"), dict) else None

            stg_batch.append((
                ai["emp_id"], ai["asset_name"], ai["asset_did"], ai["project_did"],
                wd, tdid, task.get("status", ""),
                task.get("metrics", {}).get("reqCount", 0), milestone_name,
                submitted_by, submitted_on, approved_by, approved_on, assigned_approver, RUN_ID,
            ))

        # Batch insert raw
        BATCH_SIZE = 2000
        for i in range(0, len(raw_batch), BATCH_SIZE):
            chunk = raw_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_RAW}.raw_daily_reports "
                    f"(source_type, source_id, project_did, asset_did, task_did, data, run_id, run_date) "
                    f"VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::uuid, $8) "
                    f"ON CONFLICT (source_type, source_id) DO UPDATE SET "
                    f"data = EXCLUDED.data, project_did = EXCLUDED.project_did, "
                    f"asset_did = EXCLUDED.asset_did, task_did = EXCLUDED.task_did, "
                    f"run_id = EXCLUDED.run_id, run_date = EXCLUDED.run_date, loaded_at = NOW() "
                    f"WHERE raw_daily_reports.data IS DISTINCT FROM EXCLUDED.data",
                    c,
                ),
                description=f"raw tasks batch {i // BATCH_SIZE + 1}",
            )
            logger.info(f"  Raw: {min(i + BATCH_SIZE, len(raw_batch))}/{len(raw_batch)}")

        # Batch insert staging
        for i in range(0, len(stg_batch), BATCH_SIZE):
            chunk = stg_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_STAGING}.stg_daily_reports "
                    f"(emp_id, asset_name, asset_did, project_did, work_date, task_did, task_status, "
                    f" req_count, milestone, submitted_by, submitted_on, approved_by, approved_on, assigned_approver, run_id) "
                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::uuid) "
                    f"ON CONFLICT (task_did) DO UPDATE SET "
                    f"task_status=EXCLUDED.task_status, req_count=EXCLUDED.req_count, "
                    f"submitted_by=EXCLUDED.submitted_by, submitted_on=EXCLUDED.submitted_on, "
                    f"approved_by=EXCLUDED.approved_by, approved_on=EXCLUDED.approved_on, "
                    f"assigned_approver=EXCLUDED.assigned_approver, "
                    f"run_id=EXCLUDED.run_id, loaded_at=NOW() "
                    # Skip no-op rewrites: unconditionally bumping run_id/loaded_at
                    # rewrote all ~46k window rows every 5-min run (WAL + autovacuum
                    # churn that depleted the Supabase Disk IO budget, 2026-07-09).
                    f"WHERE (stg_daily_reports.task_status, stg_daily_reports.req_count, "
                    f" stg_daily_reports.submitted_by, stg_daily_reports.submitted_on, "
                    f" stg_daily_reports.approved_by, stg_daily_reports.approved_on, "
                    f" stg_daily_reports.assigned_approver) IS DISTINCT FROM "
                    f"(EXCLUDED.task_status, EXCLUDED.req_count, EXCLUDED.submitted_by, "
                    f" EXCLUDED.submitted_on, EXCLUDED.approved_by, EXCLUDED.approved_on, "
                    f" EXCLUDED.assigned_approver)",
                    c,
                ),
                description=f"stg tasks batch {i // BATCH_SIZE + 1}",
            )
            logger.info(f"  Staging: {min(i + BATCH_SIZE, len(stg_batch))}/{len(stg_batch)}")

        logger.info(f"  Loaded {len(stg_batch)} tasks")

        # Step 4: Fetch requirements and timers separately
        all_reqs = []
        all_timers = []
        # Tasks whose child fetch SUCCEEDED this run: for these (and only
        # these) the fetched set is authoritative, so Step 5 may delete staged
        # rows Swift no longer returns (requirement/timer deleted in Swift).
        req_ok_dids = []
        timer_ok_dids = []

        # 4a: Fetch requirements (if not timers_only)
        if not timers_only and tasks_with_reqs:
            logger.info(f"\n=== Step 4a: Fetching REQUIREMENTS for {len(tasks_with_reqs)} tasks ===")
            done = 0
            def fetch_reqs(task_info):
                ai = task_info["asset_info"]
                wd = task_info["work_date"]
                task_did = task_info["task"].get("id", "")
                reqs, _, ok = self.fetch_requirement_and_timer(task_did, requirements_only=True)
                return ai, wd, task_did, reqs, ok
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(fetch_reqs, t): t for t in tasks_with_reqs}
                for future in as_completed(futures, timeout=7200):
                    ai, wd, task_did, reqs, fetch_ok = future.result()
                    if fetch_ok and task_did:
                        req_ok_dids.append(task_did)
                    for r in reqs:
                        all_reqs.append((ai, wd, task_did, r))
                    done += 1
                    if done % 500 == 0:
                        logger.info(f"  Reqs: {done}/{len(tasks_with_reqs)} tasks | {len(all_reqs)} reqs")
            logger.info(f"  Requirements fetched: {len(all_reqs)}")

        # 4b: Fetch timers (if not requirements_only) — for ALL active tasks
        if not requirements_only and tasks_for_timers:
            logger.info(f"\n=== Step 4b: Fetching TIMERS for {len(tasks_for_timers)} tasks ===")
            done = 0
            def fetch_tmrs(task_info):
                ai = task_info["asset_info"]
                wd = task_info["work_date"]
                task_did = task_info["task"].get("id", "")
                _, timers, ok = self.fetch_requirement_and_timer(task_did, timers_only=True)
                return ai, wd, task_did, timers, ok
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(fetch_tmrs, t): t for t in tasks_for_timers}
                for future in as_completed(futures, timeout=7200):
                    ai, wd, task_did, timers, fetch_ok = future.result()
                    if fetch_ok and task_did:
                        timer_ok_dids.append(task_did)
                    for t in timers:
                        all_timers.append((ai, wd, task_did, t))
                    done += 1
                    if done % 500 == 0:
                        logger.info(f"  Timers: {done}/{len(tasks_for_timers)} tasks | {len(all_timers)} timers")
            logger.info(f"  Timers fetched: {len(all_timers)}")

        logger.info(f"  Total: {len(all_reqs)} reqs, {len(all_timers)} timers")

        # Batch load requirements
        logger.info("  Loading requirements...")
        req_raw_batch = []
        req_stg_batch = []
        for ai, wd, task_did, r in all_reqs:
            hours = parse_hours(r.get("name"))
            req_stg_batch.append((
                ai["emp_id"], wd, task_did, hours,
                (r.get("description") or "")[:2000], r.get("status", ""),
                r.get("id", ""), epoch_to_dt(r.get("dateCreated")),
                epoch_to_dt(r.get("lastUpdated")),
                int((r.get("metrics") or {}).get("fileUploadedCount") or 0), RUN_ID,
            ))
            req_raw_batch.append((
                "requirement", r.get("id", ""), ai["project_did"], ai["asset_did"],
                task_did, json.dumps(r, default=str), RUN_ID, date.today(),
            ))

        for i in range(0, len(req_stg_batch), BATCH_SIZE):
            chunk = req_stg_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_STAGING}.stg_daily_report_hours "
                    f"(emp_id, work_date, task_did, hours_worked, work_description, "
                    f" req_status, req_id, created_at_api, updated_at_api, file_uploaded_count, run_id) "
                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::uuid) "
                    f"ON CONFLICT (task_did, req_id) DO UPDATE SET "
                    f"hours_worked=EXCLUDED.hours_worked, work_description=EXCLUDED.work_description, "
                    f"req_status=EXCLUDED.req_status, updated_at_api=EXCLUDED.updated_at_api, "
                    f"file_uploaded_count=EXCLUDED.file_uploaded_count, "
                    f"run_id=EXCLUDED.run_id, loaded_at=NOW() "
                    # Skip no-op rewrites (Disk IO churn; see stg_daily_reports upsert)
                    f"WHERE (stg_daily_report_hours.hours_worked, stg_daily_report_hours.work_description, "
                    f" stg_daily_report_hours.req_status, stg_daily_report_hours.updated_at_api, "
                    f" stg_daily_report_hours.file_uploaded_count) IS DISTINCT FROM "
                    f"(EXCLUDED.hours_worked, EXCLUDED.work_description, EXCLUDED.req_status, "
                    f" EXCLUDED.updated_at_api, EXCLUDED.file_uploaded_count)",
                    c,
                ),
                description=f"stg reqs batch {i // BATCH_SIZE + 1}",
            )
        for i in range(0, len(req_raw_batch), BATCH_SIZE):
            chunk = req_raw_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_RAW}.raw_daily_reports "
                    f"(source_type, source_id, project_did, asset_did, task_did, data, run_id, run_date) "
                    f"VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::uuid, $8) "
                    f"ON CONFLICT (source_type, source_id) DO UPDATE SET "
                    f"data = EXCLUDED.data, project_did = EXCLUDED.project_did, "
                    f"asset_did = EXCLUDED.asset_did, task_did = EXCLUDED.task_did, "
                    f"run_id = EXCLUDED.run_id, run_date = EXCLUDED.run_date, loaded_at = NOW() "
                    f"WHERE raw_daily_reports.data IS DISTINCT FROM EXCLUDED.data",
                    c,
                ),
                description=f"raw reqs batch {i // BATCH_SIZE + 1}",
            )
        logger.info(f"  Loaded {len(req_stg_batch)} requirements")

        # Batch load timers
        logger.info("  Loading timers...")
        tmr_raw_batch = []
        tmr_stg_batch = []
        for ai, wd, task_did, t in all_timers:
            t_start = epoch_to_dt(t.get("start"))
            t_end = epoch_to_dt(t.get("end"))
            dur = round((t_end - t_start).total_seconds() / 60, 2) if t_start and t_end else None

            tmr_stg_batch.append((
                ai["emp_id"], wd, task_did, t.get("id", ""),
                t_start, t_end, dur, t.get("byName", ""), t.get("user", ""), RUN_ID,
            ))
            tmr_raw_batch.append((
                "timer", t.get("id", ""), ai["project_did"], ai["asset_did"],
                task_did, json.dumps(t, default=str), RUN_ID, date.today(),
            ))

        for i in range(0, len(tmr_stg_batch), BATCH_SIZE):
            chunk = tmr_stg_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_STAGING}.stg_daily_report_attendance "
                    f"(emp_id, work_date, task_did, timer_id, timer_start, timer_end, "
                    f" duration_min, user_name, user_auth_id, run_id) "
                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::uuid) "
                    f"ON CONFLICT (task_did, timer_id) DO UPDATE SET "
                    f"timer_start=EXCLUDED.timer_start, timer_end=EXCLUDED.timer_end, "
                    f"duration_min=EXCLUDED.duration_min, run_id=EXCLUDED.run_id, loaded_at=NOW() "
                    # Skip no-op rewrites (Disk IO churn; see stg_daily_reports upsert)
                    f"WHERE (stg_daily_report_attendance.timer_start, stg_daily_report_attendance.timer_end, "
                    f" stg_daily_report_attendance.duration_min) IS DISTINCT FROM "
                    f"(EXCLUDED.timer_start, EXCLUDED.timer_end, EXCLUDED.duration_min)",
                    c,
                ),
                description=f"stg timers batch {i // BATCH_SIZE + 1}",
            )
        for i in range(0, len(tmr_raw_batch), BATCH_SIZE):
            chunk = tmr_raw_batch[i:i + BATCH_SIZE]
            retry_db(
                lambda c=chunk: db.executemany(
                    f"INSERT INTO {SCHEMA_RAW}.raw_daily_reports "
                    f"(source_type, source_id, project_did, asset_did, task_did, data, run_id, run_date) "
                    f"VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::uuid, $8) "
                    f"ON CONFLICT (source_type, source_id) DO UPDATE SET "
                    f"data = EXCLUDED.data, project_did = EXCLUDED.project_did, "
                    f"asset_did = EXCLUDED.asset_did, task_did = EXCLUDED.task_did, "
                    f"run_id = EXCLUDED.run_id, run_date = EXCLUDED.run_date, loaded_at = NOW() "
                    f"WHERE raw_daily_reports.data IS DISTINCT FROM EXCLUDED.data",
                    c,
                ),
                description=f"raw timers batch {i // BATCH_SIZE + 1}",
            )
        logger.info(f"  Loaded {len(tmr_stg_batch)} timers")

        # Step 5: reconcile deletions. The staging loads above are upsert-only,
        # so a requirement/timer deleted in Swift lingered in staging forever,
        # inflating req_count/total_hours in the serving views (found 2026-07-08:
        # a deleted 11h requirement kept a report at 21h in HR review). For each
        # task whose child fetch succeeded this run, anything staged beyond the
        # fetched set was deleted in Swift -- remove it. Tasks whose fetch threw
        # are skipped (ok=False; a transient API failure must never wipe rows)
        # and self-heal on a later run. Tasks reporting reqCount == 0 never
        # enter the requirements fetch at all, so their leftovers are cleared
        # from the task payload's own metric (explicit 0 only -- a task with no
        # metrics dict is never treated as empty).
        logger.info("\n=== Step 5: Reconciling deletions ===")
        stale_reqs = 0
        stale_timers = 0
        if not timers_only:
            zero_req_dids = [
                t["task"].get("id", "") for t in all_tasks
                if not t.get("status_only")
                and t["task"].get("id")
                and isinstance(t["task"].get("metrics"), dict)
                and t["task"]["metrics"].get("reqCount") == 0
            ]
            recon_dids = req_ok_dids + zero_req_dids
            seen_req_ids = [r.get("id", "") for _, _, _, r in all_reqs]
            for i in range(0, len(recon_dids), BATCH_SIZE):
                chunk = recon_dids[i:i + BATCH_SIZE]
                status = retry_db(
                    lambda c=chunk: db.execute(
                        f"WITH keep AS (SELECT unnest($2::text[]) AS req_id) "
                        f"DELETE FROM {SCHEMA_STAGING}.stg_daily_report_hours h "
                        f"WHERE h.task_did = ANY($1::text[]) "
                        f"AND NOT EXISTS (SELECT 1 FROM keep WHERE keep.req_id = h.req_id)",
                        c, seen_req_ids,
                    ),
                    description=f"reconcile reqs batch {i // BATCH_SIZE + 1}",
                )
                stale_reqs += int(status.rsplit(" ", 1)[-1])
            skipped = len(tasks_with_reqs) - len(req_ok_dids)
            logger.info(f"  Stale requirements deleted: {stale_reqs} "
                        f"({len(recon_dids)} tasks reconciled, {skipped} skipped on failed fetch)")

        if not requirements_only:
            seen_timer_ids = [t.get("id", "") for _, _, _, t in all_timers]
            for i in range(0, len(timer_ok_dids), BATCH_SIZE):
                chunk = timer_ok_dids[i:i + BATCH_SIZE]
                status = retry_db(
                    lambda c=chunk: db.execute(
                        f"WITH keep AS (SELECT unnest($2::text[]) AS timer_id) "
                        f"DELETE FROM {SCHEMA_STAGING}.stg_daily_report_attendance h "
                        f"WHERE h.task_did = ANY($1::text[]) "
                        f"AND NOT EXISTS (SELECT 1 FROM keep WHERE keep.timer_id = h.timer_id)",
                        c, seen_timer_ids,
                    ),
                    description=f"reconcile timers batch {i // BATCH_SIZE + 1}",
                )
                stale_timers += int(status.rsplit(" ", 1)[-1])
            skipped = len(tasks_for_timers) - len(timer_ok_dids)
            logger.info(f"  Stale timers deleted: {stale_timers} "
                        f"({len(timer_ok_dids)} tasks reconciled, {skipped} skipped on failed fetch)")

        # Step 6: refresh the email alias map (migration 167/169). Swift-side
        # feed of reference.ref_employee_emails: full recompute, idempotent.
        # Throttled to hourly: the harvest reads ~150 MB per call, which at the
        # rolling pipeline's 5-minute cadence was ~44 GB/day of disk reads and
        # a main driver of the Supabase Disk IO budget depletion (2026-07-09).
        # The harvest bumps last_seen on every evidenced alias, so
        # max(last_seen) marks the last successful harvest.
        # Best-effort: a harvest failure must not fail the pipeline run (the
        # next due run self-heals).
        logger.info("\n=== Step 6: Harvesting email aliases ===")
        try:
            harvest_due = retry_db(
                lambda: db.fetchval(
                    "SELECT COALESCE(max(last_seen) < now() - interval '55 minutes', true) "
                    "FROM reference.ref_employee_emails"
                ),
                description="email alias harvest due check",
            )
            if harvest_due:
                harvested = retry_db(
                    lambda: db.fetchval("SELECT reference.harvest_employee_email_aliases()"),
                    description="email alias harvest",
                )
                logger.info(f"  Alias rows upserted: {harvested}")
            else:
                logger.info("  Skipped (harvested within the last hour)")
        except Exception as exc:  # noqa: BLE001 - alias freshness is best-effort
            logger.warning(f"  Email alias harvest failed (non-fatal): {exc}")

        close_db()

        logger.info(f"\n{'='*60}")
        logger.info(f"Pipeline complete")
        logger.info(f"  Employees: {len(all_assets)}")
        logger.info(f"  Tasks: {len(stg_batch)}")
        logger.info(f"  Requirements: {len(req_stg_batch)}")
        logger.info(f"  Timers: {len(tmr_stg_batch)}")
        logger.info(f"  Stale rows removed: {stale_reqs} reqs, {stale_timers} timers")
        logger.info(f"  Run ID: {RUN_ID}")
        logger.info(f"{'='*60}")

        return {
            "tasks": len(stg_batch),
            "requirements": len(req_stg_batch),
            "timers": len(tmr_stg_batch),
            "stale_requirements_deleted": stale_reqs,
            "stale_timers_deleted": stale_timers,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Extract all tasks (not just incremental)")
    parser.add_argument("--days", type=int, help="Extract last N days only")
    args = parser.parse_args()

    logger.info("Discovering active Daily Reports projects...")
    projects = discover_projects()
    if not projects:
        print("No active Daily Reports projects found.")
        return

    pipeline = DailyReportsPipeline()
    pipeline.run(projects, full=args.full, days=args.days)


if __name__ == "__main__":
    main()
