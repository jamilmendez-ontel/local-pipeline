"""Land OnPoint daily reports in OntelDB (migration 293 tables).

From November 2026 the team files daily reports and clocks in on OnPoint
(onpoint.ontel.co) instead of Swift. This job pulls every FILED OnPoint report
(submitted / approved / rejected) for a trailing window of report dates and
upserts it into data_staging.stg_onpoint_daily_reports (+ the raw payload in
data_raw.raw_onpoint_daily_reports). Rows in the window that OnPoint no longer
lists (sent back to draft, cancelled) are deleted, in the same statement.
OnPoint users are cached in data_staging.stg_onpoint_users (email = join key
to the roster). DRMC reads these through migration 294's views.

API: OnPoint v1, PAT in ONPOINT_PAT (a time-admin token: time.approvals.list
returns everyone). time.approvals.list has no cursor and caps at 200 rows, so
the job asks one report date + one status per call and fails loudly if a call
comes back full (it would mean rows were cut off).

Usage:
    python run_onpoint_daily_reports.py              # last 14 days
    python run_onpoint_daily_reports.py --days 30
    python run_onpoint_daily_reports.py --since 2026-11-01
    python run_onpoint_daily_reports.py --dry-run    # pull + summarize, write nothing
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import get_logger, setup_logging

sys.stdout.reconfigure(line_buffering=True)
setup_logging()
log = get_logger("onpoint_daily_reports")

BASE = os.getenv("ONPOINT_API_BASE", "https://onpoint.ontel.co/api/v1/")
STATUSES = ("submitted", "approved", "rejected")
PAGE_CAP = 200          # time.approvals.list hard cap, no cursor
USER_REFRESH = timedelta(days=1)
MANILA = ZoneInfo("Asia/Manila")

REPORT_FIELDS = (
    "id", "user_id", "report_date", "status", "self_reported_hours", "worked_minutes",
    "computed_timer_hours_snapshot", "time_in", "time_out", "submitted_at", "notes",
    "review_comment", "owner_name", "created_at", "updated_at",
)


class OnPointError(RuntimeError):
    pass


def window(today: date, days: int, since: str | None) -> tuple[date, date]:
    """Report-date window: [since or today-days+1, today+1]. +1 covers a PH
    night shift whose OnPoint report_date runs ahead of the runner's date."""
    start = date.fromisoformat(since) if since else today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    if start > end:
        raise ValueError(f"window start {start} is after end {end}")
    return start, end


def call(session: requests.Session, op: str, params: dict, attempts: int = 4) -> dict:
    """GET one v1 operation with retry on 429 / 5xx / network errors."""
    for i in range(attempts):
        try:
            r = session.get(BASE + op, params=params, timeout=60)
        except requests.RequestException as e:
            err = f"{op}: {e}"
        else:
            if r.status_code == 200:
                return r.json()
            err = f"{op} {params}: HTTP {r.status_code} {r.text[:300]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise OnPointError(err)
        if i < attempts - 1:
            time.sleep(2 * 2 ** i)
    raise OnPointError(err)


def normalize(rep: dict) -> dict:
    """Validate one API report and keep the landed fields."""
    missing = [k for k in ("id", "user_id", "report_date", "status") if not rep.get(k)]
    if missing:
        raise OnPointError(f"report missing {missing}: {json.dumps(rep)[:300]}")
    if rep["status"] not in STATUSES:
        raise OnPointError(f"unexpected status {rep['status']!r} on report {rep['id']}")
    date.fromisoformat(rep["report_date"])
    return {k: rep.get(k) for k in REPORT_FIELDS}


def pull_reports(session: requests.Session, start: date, end: date) -> list[dict]:
    reports: dict[str, dict] = {}
    d = start
    while d <= end:
        for st in STATUSES:
            body = call(session, "time.approvals.list",
                        {"from": d.isoformat(), "to": d.isoformat(), "status": st, "limit": PAGE_CAP})
            rows = body.get("data")
            if not isinstance(rows, list):
                raise OnPointError(f"time.approvals.list {d} {st}: no data list")
            if len(rows) >= PAGE_CAP:
                raise OnPointError(f"time.approvals.list {d} {st} returned {len(rows)} rows (cap {PAGE_CAP}); "
                                   "rows may be cut off, refusing to load a partial day")
            for rep in rows:
                n = normalize(rep)
                if n["report_date"] != d.isoformat() or n["status"] != st:
                    raise OnPointError(f"report {n['id']} came back for {n['report_date']}/{n['status']}, "
                                       f"asked {d}/{st}")
                reports[n["id"]] = n
        d += timedelta(days=1)
    return list(reports.values())


def stale_users(db, user_ids: set[str]) -> set[str]:
    if not user_ids:
        return set()
    rows = db.fetch("select user_id::text, loaded_at from data_staging.stg_onpoint_users "
                    "where user_id = any($1::uuid[])", list(user_ids))
    fresh = {r["user_id"] for r in rows if r["loaded_at"] > datetime.now(timezone.utc) - USER_REFRESH}
    return user_ids - fresh


def pull_users(session: requests.Session, user_ids: set[str]) -> list[dict]:
    out = []
    for uid in sorted(user_ids):
        try:
            p = call(session, "users.get", {"id": uid}).get("profile") or {}
        except OnPointError as e:
            # A deleted / hidden user must not block every run: keep their reports,
            # they just will not map to an employee until users.get answers again.
            log.warning(f"users.get {uid} failed, skipping: {e}")
            continue
        if p.get("id") != uid:
            raise OnPointError(f"users.get {uid} returned profile {p.get('id')}")
        out.append({k: p.get(k) for k in ("id", "email", "name", "role", "user_type",
                                          "manager_id", "co_manager_id", "suspended_at")})
    return out


UPSERT_USERS_SQL = """
insert into data_staging.stg_onpoint_users
  (user_id, email, name, role, user_type, manager_id, co_manager_id, suspended_at, loaded_at)
select id, lower(email), name, role, user_type, manager_id, co_manager_id, suspended_at, now()
from jsonb_to_recordset($1::jsonb) as u(id uuid, email text, name text, role text, user_type text,
                                        manager_id uuid, co_manager_id uuid, suspended_at timestamptz)
on conflict (user_id) do update set
  email = excluded.email, name = excluded.name, role = excluded.role, user_type = excluded.user_type,
  manager_id = excluded.manager_id, co_manager_id = excluded.co_manager_id,
  suspended_at = excluded.suspended_at, loaded_at = excluded.loaded_at
"""

# One statement = one transaction. Data-modifying CTEs all see the same snapshot,
# so `cand` (the window's person-days OnPoint no longer lists) is computed from
# the table as it was before this statement, never from the rows upserted here.
# The upsert keys on the person-day, not report_id: a report OnPoint deleted and
# refiled comes back under a new id and must replace the old row, not collide
# with the unique (user_id, report_date). `cand` excludes every incoming
# person-day, so no row is both updated and deleted by this statement.
# Delete cap: one glitchy empty API page must not wipe a day. When the window
# would lose more than max($4 rows, $5 x its rows), nothing is deleted and the
# run says so (the next run retries).
LOAD_SQL = """
with src as (
  select r.*, j.payload
  from jsonb_array_elements($1::jsonb) as j(payload),
       jsonb_to_record(j.payload) as r(
         id uuid, user_id uuid, report_date date, status text, self_reported_hours numeric,
         worked_minutes numeric, computed_timer_hours_snapshot numeric, time_in timestamptz,
         time_out timestamptz, submitted_at timestamptz, notes text, review_comment text,
         owner_name text, created_at timestamptz, updated_at timestamptz)
),
cand as (
  select s.report_id
  from data_staging.stg_onpoint_daily_reports s
  where s.report_date between $2::date and $3::date
    and not exists (select 1 from src where src.user_id = s.user_id and src.report_date = s.report_date)
),
del_ok as (
  select (select count(*) from cand) <= greatest($4::int, $5::numeric * (
           select count(*) from data_staging.stg_onpoint_daily_reports s
           where s.report_date between $2::date and $3::date)) as allowed
),
raw_up as (
  insert into data_raw.raw_onpoint_daily_reports (report_id, payload, fetched_at)
  select id, payload, now() from src
  on conflict (report_id) do update set payload = excluded.payload, fetched_at = excluded.fetched_at
  returning 1
),
stg_up as (
  insert into data_staging.stg_onpoint_daily_reports
    (report_id, user_id, report_date, status, self_reported_hours, worked_minutes,
     computed_timer_hours, time_in, time_out, submitted_at, notes, review_comment,
     owner_name, created_at, updated_at, loaded_at)
  select id, user_id, report_date, status, self_reported_hours, round(worked_minutes)::integer,
         computed_timer_hours_snapshot, time_in, time_out, submitted_at, notes, review_comment,
         owner_name, created_at, updated_at, now()
  from src
  on conflict (user_id, report_date) do update set
    report_id = excluded.report_id, status = excluded.status,
    self_reported_hours = excluded.self_reported_hours, worked_minutes = excluded.worked_minutes,
    computed_timer_hours = excluded.computed_timer_hours, time_in = excluded.time_in,
    time_out = excluded.time_out, submitted_at = excluded.submitted_at, notes = excluded.notes,
    review_comment = excluded.review_comment, owner_name = excluded.owner_name,
    created_at = excluded.created_at, updated_at = excluded.updated_at, loaded_at = excluded.loaded_at
  returning 1
),
stg_del as (
  delete from data_staging.stg_onpoint_daily_reports s
  where s.report_id in (select report_id from cand)
    and (select allowed from del_ok)
  returning s.report_id
),
raw_del as (
  delete from data_raw.raw_onpoint_daily_reports r
  where r.report_id in (select report_id from stg_del)
  returning 1
)
select (select count(*) from stg_up) upserted, (select count(*) from stg_del) deleted,
       (select count(*) from cand) unlisted
"""
DELETE_CAP_ROWS = 10       # always allowed to delete this many
DELETE_CAP_SHARE = 0.2     # or this share of the window's rows, whichever is larger




def run(days: int, since: str | None, dry_run: bool) -> int:
    pat = os.environ.get("ONPOINT_PAT", "").strip()
    if not pat:
        raise OnPointError("ONPOINT_PAT is not set")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {pat}"

    today = datetime.now(MANILA).date()
    start, end = window(today, days, since)
    t0 = time.monotonic()
    reports = pull_reports(session, start, end)
    by_status = {s: sum(r["status"] == s for r in reports) for s in STATUSES}
    log.info(f"OnPoint {start}..{end}: {len(reports)} filed reports {by_status} "
          f"in {time.monotonic() - t0:.0f}s")
    # Outage guard: a whole window with no filed report means the API answered
    # wrong, not that nobody worked. Never let that wipe the window.
    if not reports:
        raise OnPointError(f"0 filed reports for {start}..{end}; refusing to load (would delete the window)")
    if dry_run:
        log.info("dry run: nothing written")
        return len(reports)

    from config import close_db
    from base_extractor import BaseExtractor
    tracker = None
    try:
        tracker = BaseExtractor(pipeline_name="onpoint_daily_reports")
        tracker.start_pipeline_run(metadata={"from": str(start), "to": str(end), "reports": len(reports)})
        db = tracker.db
        need = stale_users(db, {r["user_id"] for r in reports})
        users = pull_users(session, need)
        if users:
            db.execute(UPSERT_USERS_SQL, users)  # pool codec encodes jsonb
        row = db.fetchrow(LOAD_SQL, reports, start, end, DELETE_CAP_ROWS, DELETE_CAP_SHARE)
        log.info(f"loaded: {row['upserted']} upserted, {row['deleted']} deleted, {len(users)} users refreshed")
        if row["unlisted"] > row["deleted"]:
            log.warning(f"{row['unlisted']} stored reports in {start}..{end} are no longer listed by OnPoint; "
                        f"over the delete cap, so none were deleted this run")
        tracker.complete_pipeline_run("success", records=row["upserted"])
    except Exception as e:
        if tracker is not None:
            try:
                tracker.complete_pipeline_run("failed", error=str(e)[:1000])
            except Exception as e2:  # never mask the original error
                log.error(f"could not record the failed run: {e2}")
        raise
    finally:
        close_db()
    return len(reports)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="trailing report-date window (default 14)")
    ap.add_argument("--since", help="start date YYYY-MM-DD (overrides --days)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-email", action="store_true", help="no failure email (local runs)")
    a = ap.parse_args()
    if a.days < 1:
        ap.error("--days must be >= 1")
    if a.dry_run or a.no_email:
        run(a.days, a.since, a.dry_run)
        return
    from main import run_pipeline_with_notification
    ok = run_pipeline_with_notification(lambda: run(a.days, a.since, False), "OnPoint Daily Reports",
                                        logger_prefixes=["pipeline.onpoint_daily_reports", "pipeline.base",
                                                         "pipeline.db"],
                                        recipients=["jamil.mendez@ontel.co"], email_on_success=False)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
