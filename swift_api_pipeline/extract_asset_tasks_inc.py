#!/usr/bin/env python3
"""Incremental asset-tasks pipeline (TS13+ shadow pilot).

Walk: project (content_watermarks skip) -> asset list (stg_assets_inc is
the watermark) -> task list for changed assets only -> guarded upserts of
changed tasks + keep-list reconcile inside every successfully-fetched
scope. See docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md
and the API findings doc for field semantics.
"""
import argparse
import json
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import SCHEMA_PIPELINE, SCHEMA_REFERENCE, setup_logging
from db_tx import close_tx_db, get_tx_db, retry_tx_db
from extract import SwiftAPIExtractor
from extract_daily_reports import DailyReportsPipeline
from transform import clean_task_name

logger = logging.getLogger("pipeline.inc_asset_tasks")

# FIELD_MAP: single place where Swift payload keys are named. MUST match
# docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md; update
# here if the probe found different names.
#
# Note: there is no "assetId" key on asset-project rows. asset_identifier
# is sparse (5,022 of 5,025 rows in the probe); missing means None.
FIELD_MAP = {
    "id": "id",
    "last_updated": "lastUpdated",
    "collection": "collection",
    "asset_name": "name",
    "asset_identifier": "identifier",
    "req_count": ("metrics", "reqCount"),
    "task_count": ("metrics", "taskCount"),
}


def epoch_to_ts(val):
    """Swift epoch millis -> aware UTC datetime (None-safe)."""
    if not val:
        return None
    return datetime.fromtimestamp(int(val) / 1000, tz=timezone.utc)


def plan_asset_visits(fetched_assets, stored_assets):
    """Decide which assets to descend into.

    stored_assets: {asset_did: {"last_updated": datetime|None, ...}} from
    stg_assets_inc for this project. Returns (visits, missing_dids):
    visits = fetched asset rows that are new or moved past the stored
    last_updated; missing_dids = stored dids absent from the fetch
    (deletion candidates, reconciled by the caller).
    """
    visits = []
    fetched_ids = set()
    for a in fetched_assets:
        did = a.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored = stored_assets.get(did)
        fetched_ts = epoch_to_ts(a.get(FIELD_MAP["last_updated"]))
        # Deliberate bias: an entity with a missing/unparseable lastUpdated
        # is ALWAYS visited/written. Fail toward extra work, never staleness.
        if stored is None or stored.get("last_updated") is None \
                or fetched_ts is None or fetched_ts > stored["last_updated"]:
            visits.append(a)
    missing = set(stored_assets) - fetched_ids
    return visits, missing


def plan_task_writes(fetched_tasks, stored_task_ts):
    """Same contract at task level. stored_task_ts: {task_did: last_updated}.

    The asset-tasks listing mixes collections: milestone rows repeat under
    every asset and are the only id duplicates. Only rows with
    collection == "asset-tasks" are considered here; every other
    collection (milestones, etc.) is ignored entirely, not written and
    not counted as fetched for deletion detection.
    """
    writes = []
    fetched_ids = set()
    for t in fetched_tasks:
        if t.get(FIELD_MAP["collection"]) != "asset-tasks":
            continue
        did = t.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored_ts = stored_task_ts.get(did)
        fetched_ts = epoch_to_ts(t.get(FIELD_MAP["last_updated"]))
        if stored_ts is None or fetched_ts is None or fetched_ts > stored_ts:
            writes.append(t)
    missing = set(stored_task_ts) - fetched_ids
    return writes, missing


# ---------------------------------------------------------------------------
# Walker IO: fetcher, field-path mapping, guarded upserts, keep-list reconcile
#
# Field paths verified live against TS17 2026-07-09 (throwaway probe, keys
# only, deleted after use). See "Task 5 mapping notes" in
# docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md for the
# full table and the assignedTo/email sparsity finding.
# ---------------------------------------------------------------------------

TZ_ET = ZoneInfo("America/New_York")

BATCH_SIZE = 500


class IncFetcher(DailyReportsPipeline):
    """DailyReportsPipeline's fetch_assets/fetch_tasks/_request, without its
    DB dependency. BaseExtractor.__init__ (used by DailyReportsPipeline)
    calls get_db(), which needs the direct DB host (IPv6-only, requires
    Cloudflare WARP). This walker only needs the API side here -- all DB
    access goes through the caller's get_tx_db() facade (transaction pooler,
    no WARP needed) -- so authenticate via SwiftAPIExtractor instead, same
    as probe_inc_asset_tasks.py's ProbeFetcher. _request()'s 401 retry path
    (self.ext.authenticate() / self.ext.token) works unchanged.
    """

    def __init__(self):
        ex = SwiftAPIExtractor()
        ex.authenticate()
        self.ext = ex
        self.headers = {"Authorization": f"Bearer {ex.token}"}
        self.base = ex.base_url


_fetcher_lock = threading.Lock()
_fetcher = None


def _get_fetcher():
    """Shared IncFetcher singleton so concurrent per-project walks (Task 6's
    ThreadPoolExecutor) authenticate once instead of once per project."""
    global _fetcher
    if _fetcher is None:
        with _fetcher_lock:
            if _fetcher is None:
                _fetcher = IncFetcher()
    return _fetcher


def _get_field(d, spec):
    """Resolve a FIELD_MAP value against dict d: spec is either a plain key
    or an (outer, inner) tuple path (e.g. FIELD_MAP["req_count"])."""
    if isinstance(spec, tuple):
        outer, inner = spec
        sub = d.get(outer)
        return sub.get(inner) if isinstance(sub, dict) else None
    return d.get(spec)


def epoch_to_et_date(val):
    """Swift epoch millis -> America/New_York calendar date (None-safe).

    Matches transform.py's SQL date semantics for stg_asset_tasks
    (TO_TIMESTAMP(...) AT TIME ZONE 'America/New_York')::date, so
    stg_asset_tasks_inc's date columns are directly diffable by the drift
    audit against the current pipeline's output.
    """
    dt = epoch_to_ts(val)
    if dt is None:
        return None
    return dt.astimezone(TZ_ET).date()


def _person_fields(d):
    """Extract (did, collection, name, email) from a nested personnel/entity
    dict (assignedTo, submittedBy, approvedBy, cancelledBy). Confirmed live
    (2026-07-09 probe): submittedBy/approvedBy/cancelledBy are personnel
    records and reliably carry id/collection/name/email together whenever
    populated. assignedTo can reference a non-personnel entity (its 'type'
    sub-dict differs) and never carried an 'email' key in the probe sample
    (0/67 populated rows) -- the path is real, the field is just sparse for
    that reason. See mapping notes in the findings doc.
    """
    if not isinstance(d, dict) or not d:
        return None, None, None, None
    return d.get("id"), d.get("collection"), d.get("name"), d.get("email")


STG_COLS = (
    "task_did, project_did, project_status, asset_did, asset_id, asset_name, "
    "asset_requirement_count, task_name, task_status, task_scheduled, "
    "task_assigned_to_did, task_assigned_to_collection, task_assigned_to_name, "
    "task_assigned_to_email, task_submitted_on, task_submitted_by_did, "
    "task_submitted_by_name, task_submitted_by_email, task_approved_on, "
    "task_approved_by_did, task_approved_by_name, task_approved_by_email, "
    "task_cancelled_on, task_cancelled_by_did, task_cancelled_by_name, "
    "task_cancelled_by_email, task_name_clean, last_updated"
)

# Guarded upsert: only rewrite when business payload OR last_updated moved.
UPSERT_TASK = f"""
INSERT INTO data_staging.stg_asset_tasks_inc ({STG_COLS}, loaded_at)
VALUES ({",".join(f"${i}" for i in range(1, 29))}, now())
ON CONFLICT (task_did) DO UPDATE SET
  project_status=EXCLUDED.project_status, asset_id=EXCLUDED.asset_id,
  asset_name=EXCLUDED.asset_name,
  asset_requirement_count=EXCLUDED.asset_requirement_count,
  task_name=EXCLUDED.task_name, task_status=EXCLUDED.task_status,
  task_scheduled=EXCLUDED.task_scheduled,
  task_assigned_to_did=EXCLUDED.task_assigned_to_did,
  task_assigned_to_collection=EXCLUDED.task_assigned_to_collection,
  task_assigned_to_name=EXCLUDED.task_assigned_to_name,
  task_assigned_to_email=EXCLUDED.task_assigned_to_email,
  task_submitted_on=EXCLUDED.task_submitted_on,
  task_submitted_by_did=EXCLUDED.task_submitted_by_did,
  task_submitted_by_name=EXCLUDED.task_submitted_by_name,
  task_submitted_by_email=EXCLUDED.task_submitted_by_email,
  task_approved_on=EXCLUDED.task_approved_on,
  task_approved_by_did=EXCLUDED.task_approved_by_did,
  task_approved_by_name=EXCLUDED.task_approved_by_name,
  task_approved_by_email=EXCLUDED.task_approved_by_email,
  task_cancelled_on=EXCLUDED.task_cancelled_on,
  task_cancelled_by_did=EXCLUDED.task_cancelled_by_did,
  task_cancelled_by_name=EXCLUDED.task_cancelled_by_name,
  task_cancelled_by_email=EXCLUDED.task_cancelled_by_email,
  task_name_clean=EXCLUDED.task_name_clean,
  last_updated=EXCLUDED.last_updated, loaded_at=now()
WHERE stg_asset_tasks_inc.last_updated IS DISTINCT FROM EXCLUDED.last_updated
   OR (stg_asset_tasks_inc.task_status, stg_asset_tasks_inc.task_name)
      IS DISTINCT FROM (EXCLUDED.task_status, EXCLUDED.task_name)
"""

# Guarded upsert for the asset-level watermark store.
UPSERT_ASSET = """
INSERT INTO data_staging.stg_assets_inc
  (asset_did, project_did, asset_id, asset_name, asset_requirement_count,
   last_updated, loaded_at)
VALUES ($1, $2, $3, $4, $5, $6, now())
ON CONFLICT (asset_did) DO UPDATE SET
  project_did=EXCLUDED.project_did, asset_id=EXCLUDED.asset_id,
  asset_name=EXCLUDED.asset_name,
  asset_requirement_count=EXCLUDED.asset_requirement_count,
  last_updated=EXCLUDED.last_updated, loaded_at=now()
WHERE (stg_assets_inc.last_updated, stg_assets_inc.asset_name,
       stg_assets_inc.asset_requirement_count)
      IS DISTINCT FROM
      (EXCLUDED.last_updated, EXCLUDED.asset_name,
       EXCLUDED.asset_requirement_count)
"""

# Guarded upsert for the raw payload archive; guard purely on payload
# equality (raw rows have no separate business columns to diff on).
UPSERT_RAW_TASK = """
INSERT INTO data_raw.raw_asset_tasks_inc
  (task_did, asset_did, project_did, data, last_updated, loaded_at)
VALUES ($1, $2, $3, $4::jsonb, $5, now())
ON CONFLICT (task_did) DO UPDATE SET
  asset_did=EXCLUDED.asset_did, project_did=EXCLUDED.project_did,
  data=EXCLUDED.data, last_updated=EXCLUDED.last_updated, loaded_at=now()
WHERE raw_asset_tasks_inc.data IS DISTINCT FROM EXCLUDED.data
"""

# Watermark advance: guarded so a walk that saw nothing new (e.g. baseline
# re-run) never rewrites the row.
UPSERT_WATERMARK = """
INSERT INTO pipeline.content_watermarks (pipeline_name, content_hash, updated_at)
VALUES ($1, $2, now())
ON CONFLICT (pipeline_name) DO UPDATE SET
  content_hash=EXCLUDED.content_hash, updated_at=now()
WHERE content_watermarks.content_hash IS DISTINCT FROM EXCLUDED.content_hash
"""


def task_to_stg_row(project, asset, task):
    """Map (project, asset, task) dicts from the Swift hierarchy API into a
    tuple matching STG_COLS' column order for stg_asset_tasks_inc.

    project: dict with "project_did" (required) and "status" (optional,
    the project's own status, denormalized onto every task row as
    project_status).
    asset: one row from fetch_assets(project_did) (asset-project level);
    its metrics.reqCount and identifier/name are denormalized onto every
    task under it, same as the existing stg_asset_tasks pipeline.
    task: one row from fetch_tasks(asset_project_id) with
    collection == "asset-tasks" (callers filter via plan_task_writes /
    the same check before calling this).
    """
    assigned_did, assigned_coll, assigned_name, assigned_email = _person_fields(
        task.get("assignedTo"))
    sub_did, sub_coll, sub_name, sub_email = _person_fields(task.get("submittedBy"))
    app_did, app_coll, app_name, app_email = _person_fields(task.get("approvedBy"))
    can_did, can_coll, can_name, can_email = _person_fields(task.get("cancelledBy"))

    return (
        task.get(FIELD_MAP["id"]),                                    # task_did
        project["project_did"],                                       # project_did
        project.get("status"),                                        # project_status
        asset.get(FIELD_MAP["id"]),                                    # asset_did
        asset.get(FIELD_MAP["asset_identifier"]),                      # asset_id
        asset.get(FIELD_MAP["asset_name"]),                            # asset_name
        _get_field(asset, FIELD_MAP["req_count"]),                     # asset_requirement_count
        task.get("name"),                                              # task_name
        task.get("status"),                                           # task_status
        epoch_to_et_date(task.get("scheduled")),                       # task_scheduled
        assigned_did, assigned_coll, assigned_name, assigned_email,    # task_assigned_to_*
        epoch_to_et_date(task.get("submittedOn")),                     # task_submitted_on
        sub_did, sub_name, sub_email,                                  # task_submitted_by_*
        epoch_to_et_date(task.get("approvedOn")),                      # task_approved_on
        app_did, app_name, app_email,                                  # task_approved_by_*
        epoch_to_et_date(task.get("cancelledOn")),                     # task_cancelled_on
        can_did, can_name, can_email,                                  # task_cancelled_by_*
        clean_task_name(task.get("name")),                             # task_name_clean
        epoch_to_ts(task.get(FIELD_MAP["last_updated"])),               # last_updated
    )


def _executemany_batched(db, query, rows, description="batch upsert"):
    """executemany in batches of BATCH_SIZE; no-op for an empty row list.

    Every statement in this module is retry-safe (guarded upserts and
    keyed deletes are idempotent), so all DB calls in the walk go through
    retry_tx_db: a transient pooler connection drop must cost a retry, not
    a whole project's walk (seen live on the 2026-07-09 baseline run:
    ConnectionDoesNotExistError mid-walk failed all of TS19)."""
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        retry_tx_db(lambda b=batch: db.executemany(query, b),
                    description=description)


def walk_project(db, project, baseline=False, asset_workers=4):
    """Walk one project: watermark skip check -> assets -> per-visited-asset
    tasks, with guarded upserts and keep-list reconcile inside every scope
    whose fetch succeeded. A failed fetch NEVER triggers a reconcile for
    that scope (fail toward keeping stale rows, never toward wiping live
    ones on a transient API error).

    project: dict with "project_did" (required), "status" (optional,
    project-level status), "lastUpdated" (optional epoch millis from the
    org projects listing; used only for the skip check below).
    baseline: when True, skip the watermark check and visit/write every
    fetched asset/task regardless of its lastUpdated (still guarded on
    write, so re-running baseline is a no-op for rows that did not change).
    asset_workers: concurrent per-asset fetch/write workers within this
    project. Assets are independent scopes (own fetch, own reconcile), and
    the Swift asset-tasks endpoint takes 3-7s per call (measured
    2026-07-09), so serial per-asset processing would put a baseline walk
    of a ~5,000-asset project into double-digit hours.

    Returns a stats dict:
      {"skipped": True} -- nothing fetched, watermark unchanged.
      {"ok": False} -- the project-level asset fetch failed; no writes/
        deletes happened at all for this project.
      {"ok": True, "assets": N, "visited": N, "task_writes": N,
       "task_deletes": N} -- ok is False here (but stats still meaningful)
        if any per-asset task fetch failed; the watermark is NOT advanced
        in that case.
    """
    project_did = project["project_did"]
    watermark_name = f"asset_tasks_inc/{project_did}"
    fetcher = _get_fetcher()

    if not baseline:
        prev_row = retry_tx_db(
            lambda: db.fetchrow(
                "SELECT content_hash FROM pipeline.content_watermarks WHERE pipeline_name = $1",
                watermark_name,
            ),
            description=f"[{project_did}] watermark check",
        )
        prev = int(prev_row["content_hash"]) if prev_row and prev_row["content_hash"] else None
        proj_last_updated = project.get(FIELD_MAP["last_updated"])
        if prev is not None and proj_last_updated is not None and int(proj_last_updated) <= prev:
            return {"skipped": True}

    try:
        assets = fetcher.fetch_assets(project_did)
    except Exception as e:
        logger.warning(f"[{project_did}] fetch_assets failed: {e}")
        return {"ok": False}

    stored_asset_rows = retry_tx_db(
        lambda: db.fetch(
            "SELECT asset_did, last_updated FROM data_staging.stg_assets_inc WHERE project_did = $1",
            project_did,
        ),
        description=f"[{project_did}] stored assets",
    )
    stored_assets = {r["asset_did"]: {"last_updated": r["last_updated"]} for r in stored_asset_rows}

    visits, missing_assets = plan_asset_visits(assets, stored_assets)
    if baseline:
        visits = assets

    max_last_updated = 0  # epoch millis; max seen across asset + task rows this walk

    # Step 5: guarded upsert of ALL fetched assets (not just visited ones --
    # an asset's own metrics can move without any task under it moving).
    asset_rows = []
    for a in assets:
        did = a.get(FIELD_MAP["id"])
        if not did:
            continue
        raw_lu = a.get(FIELD_MAP["last_updated"])
        if raw_lu:
            max_last_updated = max(max_last_updated, int(raw_lu))
        asset_rows.append((
            did, project_did,
            a.get(FIELD_MAP["asset_identifier"]),
            a.get(FIELD_MAP["asset_name"]),
            _get_field(a, FIELD_MAP["req_count"]),
            epoch_to_ts(raw_lu),
        ))
    _executemany_batched(db, UPSERT_ASSET, asset_rows,
                         description=f"[{project_did}] asset upserts")

    if missing_assets:
        missing_list = list(missing_assets)
        for stmt in (
            "DELETE FROM data_staging.stg_asset_tasks_inc WHERE asset_did = ANY($1::text[])",
            "DELETE FROM data_raw.raw_asset_tasks_inc WHERE asset_did = ANY($1::text[])",
            "DELETE FROM data_staging.stg_assets_inc WHERE asset_did = ANY($1::text[])",
        ):
            retry_tx_db(lambda s=stmt: db.execute(s, missing_list),
                        description=f"[{project_did}] asset reconcile delete")

    # Step 6: per visited asset, fetch tasks and reconcile. Each asset is a
    # self-contained scope (own fetch, own upserts, own keep-list), so the
    # per-asset work runs on an inner worker pool; results are reduced from
    # return values, no shared mutable state between workers.
    def _process_asset(a):
        """Returns None (no id), {"failed": True} (fetch failed, scope
        untouched), or {"failed": False, "max_lu", "writes", "deletes"}."""
        asset_did = a.get(FIELD_MAP["id"])
        if not asset_did:
            return None
        try:
            tasks = fetcher.fetch_tasks(asset_did)
        except Exception as e:
            logger.warning(f"[{project_did}] fetch_tasks failed for asset {asset_did}: {e}")
            return {"failed": True}  # no reconcile for this asset's tasks

        # Track the max lastUpdated across every real asset-task row seen
        # (not just written ones), same conservative approach as assets.
        local_max = 0
        for t in tasks:
            if t.get(FIELD_MAP["collection"]) != "asset-tasks":
                continue
            raw_lu = t.get(FIELD_MAP["last_updated"])
            if raw_lu:
                local_max = max(local_max, int(raw_lu))

        stored_task_rows = retry_tx_db(
            lambda: db.fetch(
                "SELECT task_did, last_updated FROM data_staging.stg_asset_tasks_inc WHERE asset_did = $1",
                asset_did,
            ),
            description=f"[{project_did}] stored tasks",
        )
        stored_task_ts = {r["task_did"]: r["last_updated"] for r in stored_task_rows}

        writes, missing = plan_task_writes(tasks, stored_task_ts)
        if baseline:
            writes = [t for t in tasks if t.get(FIELD_MAP["collection"]) == "asset-tasks"]

        stg_rows = []
        raw_rows = []
        for t in writes:
            stg_rows.append(task_to_stg_row(project, a, t))
            raw_rows.append((
                t.get(FIELD_MAP["id"]), asset_did, project_did,
                json.dumps(t, default=str),
                epoch_to_ts(t.get(FIELD_MAP["last_updated"])),
            ))
        _executemany_batched(db, UPSERT_TASK, stg_rows,
                             description=f"[{project_did}] task upserts")
        _executemany_batched(db, UPSERT_RAW_TASK, raw_rows,
                             description=f"[{project_did}] raw task upserts")

        deletes = 0
        if missing:
            missing_list = list(missing)
            for stmt in (
                "DELETE FROM data_staging.stg_asset_tasks_inc WHERE task_did = ANY($1::text[])",
                "DELETE FROM data_raw.raw_asset_tasks_inc WHERE task_did = ANY($1::text[])",
            ):
                retry_tx_db(lambda s=stmt: db.execute(s, missing_list),
                            description=f"[{project_did}] task reconcile delete")
            deletes = len(missing_list)
        return {"failed": False, "max_lu": local_max,
                "writes": len(writes), "deletes": deletes}

    task_writes_total = 0
    task_deletes_total = 0
    any_task_fetch_failed = False
    done = 0
    with ThreadPoolExecutor(max_workers=asset_workers) as pool:
        for res in pool.map(_process_asset, visits):
            done += 1
            if done % 500 == 0:
                logger.info(f"[{project_did}] {done}/{len(visits)} assets processed")
            if res is None:
                continue
            if res["failed"]:
                any_task_fetch_failed = True
                continue
            max_last_updated = max(max_last_updated, res["max_lu"])
            task_writes_total += res["writes"]
            task_deletes_total += res["deletes"]

    ok = not any_task_fetch_failed
    if ok:
        retry_tx_db(
            lambda: db.execute(UPSERT_WATERMARK, watermark_name, str(max_last_updated)),
            description=f"[{project_did}] watermark advance",
        )

    return {
        "ok": ok,
        "assets": len(assets),
        "visited": len(visits),
        "task_writes": task_writes_total,
        "task_deletes": task_deletes_total,
    }


# ---------------------------------------------------------------------------
# Runner: project resolution, worker pool, CLI (Task 6)
# ---------------------------------------------------------------------------

PIPELINE_NAME = "asset_tasks_inc"

# Phase-1 pilot scope (decided by Jamil 2026-07-09): three active projects.
# Phase 2 after a clean audit week: pass --projects all13.
PILOT_PROJECTS = "TS17,TS18,TS19"


def _project_tail(project_name):
    """'TECH-OPS: TS17' -> 'TS17' (the --projects matching token)."""
    return project_name.split(":")[-1].strip().upper()


def resolve_projects(db, projects_arg):
    """Resolve --projects against reference.ref_ontel_techops_projects.

    projects_arg: comma-separated project-name tails ('TS17,TS18,TS19'),
    or 'all13' for every project_number >= 13 (phase 2). Unknown tails are
    an error, not a silent skip: a typo must not quietly shrink the pilot.
    """
    rows = db.fetch(
        f"SELECT project_did, project_name, project_number "
        f"FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects "
        f"WHERE project_number >= 13 ORDER BY project_number"
    )
    if projects_arg.strip().lower() == "all13":
        return [dict(r) for r in rows]
    wanted = {t.strip().upper() for t in projects_arg.split(",") if t.strip()}
    selected = [dict(r) for r in rows if _project_tail(r["project_name"]) in wanted]
    missing = wanted - {_project_tail(r["project_name"]) for r in selected}
    if missing:
        raise SystemExit(
            f"--projects entries not found in ref_ontel_techops_projects "
            f"(project_number >= 13): {sorted(missing)}"
        )
    return selected


# content_watermarks row that caches the org owning the walked projects, so
# recurring runs list ONE org's projects instead of sweeping all ~300 orgs
# the user can see (the sweep measured ~20 min on 2026-07-09; the pilot org
# sits late in the listing, so the early-exit alone doesn't help).
ORG_CACHE_WATERMARK = "asset_tasks_inc/_projects_org"


def fetch_project_listing(fetcher, wanted_dids=None, db=None):
    """One org-projects listing pass: {project id: project row}. The rows
    carry the project-level lastUpdated (the watermark skip signal) and
    status (denormalized onto task rows, same as the current pipeline).

    wanted_dids: when given, stop enumerating orgs as soon as every wanted
    project has been seen; a project the sweep never finds is treated as
    changed by the caller either way.

    db: when given (with wanted_dids), the single org that owns every
    wanted project is remembered in pipeline.content_watermarks under
    ORG_CACHE_WATERMARK, and the next run tries that org first (one API
    call). Stale or incomplete cache (project moved, scope widened to a
    project in another org) falls back to the full sweep and re-caches.
    """
    wanted = set(wanted_dids) if wanted_dids else None

    def _org_projects(org_id):
        got = {}
        for p in fetcher.ext.extract_projects(org_id):
            pid = p.get(FIELD_MAP["id"])
            if pid:
                got[pid] = p
        return got

    if db is not None and wanted:
        cached = retry_tx_db(
            lambda: db.fetchrow(
                "SELECT content_hash FROM pipeline.content_watermarks WHERE pipeline_name = $1",
                ORG_CACHE_WATERMARK,
            ),
            description="org cache read",
        )
        org_id = cached["content_hash"] if cached else None
        if org_id:
            try:
                listing = _org_projects(org_id)
            except Exception as e:
                logger.warning(f"org-cache fast path failed, full sweep: {e}")
                listing = {}
            if wanted <= listing.keys():
                return listing
            logger.info("org cache stale/incomplete; falling back to full org sweep")

    listing = {}
    contributing_orgs = set()
    for org in fetcher.ext.extract_organizations():
        org_projects = _org_projects(org["id"])
        listing.update(org_projects)
        if wanted and wanted & org_projects.keys():
            contributing_orgs.add(org["id"])
        if wanted and wanted <= listing.keys():
            break
    # Only cache when ONE org owns every wanted project; a multi-org scope
    # can't be served by the single-org fast path, so caching would just
    # force a stale-detect + re-sweep every run.
    if db is not None and wanted and wanted <= listing.keys() \
            and len(contributing_orgs) == 1:
        org_id = next(iter(contributing_orgs))
        retry_tx_db(
            lambda: db.execute(UPSERT_WATERMARK, ORG_CACHE_WATERMARK, org_id),
            description="org cache write",
        )
    return listing


def _start_run(db, run_id, metadata):
    """pipeline_runs INSERT, same shape as base_extractor.start_pipeline_run
    but through db_tx (copied statement rather than importing the
    session-pool loader, per the plan)."""
    db.execute(
        f"INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs "
        f"(run_id, pipeline_name, status, started_at, metadata) "
        f"VALUES ($1, $2, $3, $4, $5)",
        run_id, PIPELINE_NAME, "running", datetime.now(timezone.utc), metadata,
    )


def _complete_run(db, run_id, status, records=None, error=None):
    db.execute(
        f"UPDATE {SCHEMA_PIPELINE}.pipeline_runs "
        f"SET status = $1, completed_at = $2, records_extracted = $3, "
        f"error_message = $4 WHERE run_id = $5",
        status, datetime.now(timezone.utc), records, error, run_id,
    )


def main():
    ap = argparse.ArgumentParser(
        description="Incremental asset-tasks shadow pipeline (TS13+ pilot)")
    ap.add_argument("--baseline", action="store_true",
                    help="Seed run: skip watermark checks, visit every asset, "
                         "write every task (upserts stay guarded)")
    ap.add_argument("--full-walk", action="store_true",
                    help="Weekly ghost sweep: baseline fetch semantics with "
                         "guards, so unchanged rows are no-ops but deletions "
                         "are reconciled everywhere")
    ap.add_argument("--workers", type=int, default=6,
                    help="Concurrent project walks")
    ap.add_argument("--asset-workers", type=int, default=4,
                    help="Concurrent per-asset fetch/write workers within "
                         "each project walk")
    ap.add_argument("--projects", default=PILOT_PROJECTS,
                    help=f"Comma list of project tails (default: "
                         f"{PILOT_PROJECTS}), or 'all13' for all "
                         f"project_number >= 13")
    args = ap.parse_args()

    setup_logging()
    t0 = time.monotonic()
    # --full-walk shares baseline's walk semantics; the guarded upserts make
    # re-writes of unchanged rows no-ops, so the only real work it adds over
    # an incremental run is fetch volume + reconcile coverage.
    baseline = args.baseline or args.full_walk
    mode = ("baseline" if args.baseline
            else "full-walk" if args.full_walk else "incremental")

    db = get_tx_db()
    projects = resolve_projects(db, args.projects)
    tails = [_project_tail(p["project_name"]) for p in projects]
    logger.info(f"Mode: {mode}; {len(projects)} project(s): {', '.join(tails)}")

    fetcher = _get_fetcher()
    listing = fetch_project_listing(
        fetcher, wanted_dids=[p["project_did"] for p in projects], db=db)

    run_id = str(uuid.uuid4())
    retry_tx_db(
        lambda: _start_run(db, run_id, {"mode": mode, "projects": tails}),
        description="insert pipeline_runs",
    )

    def _walk(proj):
        row = listing.get(proj["project_did"])
        # Missing from the org listing -> treat as changed: lastUpdated None
        # falls through walk_project's skip check and the project is walked.
        project = {
            "project_did": proj["project_did"],
            "status": row.get("status") if row else None,
            "lastUpdated": row.get(FIELD_MAP["last_updated"]) if row else None,
        }
        return walk_project(db, project, baseline=baseline,
                            asset_workers=args.asset_workers)

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_walk, p): p for p in projects}
        for fut in as_completed(futures):
            tail = _project_tail(futures[fut]["project_name"])
            try:
                stats = fut.result()
            except Exception as e:
                logger.error(f"[{tail}] walk failed: {type(e).__name__}: {e}")
                stats = {"ok": False}
            results[tail] = stats
            logger.info(f"[{tail}] {stats}")

    skipped = sorted(t for t, s in results.items() if s.get("skipped"))
    failed = sorted(t for t, s in results.items() if s.get("ok") is False)
    walked = [s for s in results.values() if "assets" in s]
    total_visited = sum(s["visited"] for s in walked)
    total_writes = sum(s["task_writes"] for s in walked)
    total_deletes = sum(s["task_deletes"] for s in walked)
    elapsed = time.monotonic() - t0

    status = "completed" if not failed else "failed"
    error = f"projects with failed fetches: {', '.join(failed)}" if failed else None
    retry_tx_db(
        lambda: _complete_run(db, run_id, status, total_writes, error),
        description="update pipeline_runs",
    )

    logger.info(
        f"Done in {elapsed:.1f}s: {len(projects)} project(s), "
        f"{len(skipped)} skipped, {len(walked)} walked, "
        f"{total_visited} assets visited, {total_writes} task writes, "
        f"{total_deletes} task deletes"
        + (f", FAILED: {', '.join(failed)}" if failed else "")
    )
    close_tx_db()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
