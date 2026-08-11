#!/usr/bin/env python3
"""Doctrine audit: stg_asset_tasks (export-fed CURRENT) vs
stg_asset_tasks_inc (walk-fed INC).

DOCTRINE (approved by Jamil 2026-07-20): the export is NOT the standard of
truth — it has two proven defect classes (see
docs/2026-07-13-swift-export-data-loss-root-cause.md). The audit therefore
does not require equality; it requires every difference to be EXPLAINED:

  1. Rows in CURRENT missing from INC pass only if attributable to a GHOST
     ASSET — an asset with zero rows in INC (the walk cannot see it because
     it left the live hierarchy; the export keeps emitting its template
     rows). A missing row whose asset IS in INC = UNEXPLAINED -> FAIL.
  2. Rows in INC absent from CURRENT are the ad-hoc-task class (the export
     structurally drops them): allowed, counted, reported. Never a failure.
  3. Column diffs on shared rows are tolerated only when the ONLY differing
     columns are task_submitted_by_did / task_approved_by_did AND the
     corresponding *_by_name is NULL on both sides (dangling user reference
     in the export). Any other differing column = UNEXPLAINED -> FAIL.
  4. LIFECYCLE LAG (approved by Jamil 2026-08-10): the walk prunes at the
     asset level, but assign/schedule edits bump only the TASK lastUpdated,
     so INC lags CURRENT on assignment columns until the weekly full-walk
     sweep; conversely INC walks hourly while CURRENT reloads nightly, so
     INC legitimately LEADS on submit/approve for up to a day. A diff row
     is tolerated as lifecycle_lag when every differing column is
     lifecycle-class AND: assignment/schedule columns may differ either
     way; task_status and the submit/approve/cancel event columns must be
     strictly AHEAD on INC (the walker seeing an asset-touching event LATE
     is a bug, never lag); differing event dates on INC are within
     LAG_CAP_DAYS. The whole tolerance is active only while the sweep is
     alive (last successful baseline/full-walk within SWEEP_FRESH_DAYS) —
     a dead sweep means the bound is gone, so the gate goes strict again.

Hash match remains the trivial-pass fast path. EVERY run still inserts one
row per project into pipeline.inc_audit_results (samples LIMIT 50/20 as
before); notes now carries the doctrine verdict with TRUE counts.

Exit 0 when every project passes the doctrine, 1 otherwise.

CAVEAT (also printed on every run): the two pipelines run at different
times, so small drift right after either run is expected and self-corrects
by the next audit (timing drift shows up as UNEXPLAINED for one audit and
clears on the next; only persistent unexplained drift is the bug signal).
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

from config import SCHEMA_PIPELINE, SCHEMA_STAGING, setup_logging, get_logger
from db_tx import close_tx_db, get_tx_db, retry_tx_db
from extract_asset_tasks_inc import (PILOT_PROJECTS, PIPELINE_NAME,
                                     _project_tail, resolve_projects)

logger = get_logger("audit_asset_tasks_inc")

CURRENT = f"{SCHEMA_STAGING}.stg_asset_tasks"
INC = f"{SCHEMA_STAGING}.stg_asset_tasks_inc"

# The tx-pooler pool's default command_timeout (300 s) killed every audit
# between 18:23 and 23:27 UTC on 2026-07-20 while heavy walk UPDATEs had
# the instance IO-throttled: TimeoutError on the first HASH_SQL fetch, zero
# rows persisted — and non-strict runs stayed green (continue-on-error), so
# the outage was invisible. These comparisons legitimately take minutes on
# a busy instance; give them explicit headroom instead of the pool default.
AUDIT_QUERY_TIMEOUT = 900

# Business columns shared by both tables (everything except each side's
# bookkeeping: run_id/loaded_at/last_updated) minus the join/filter keys
# (task_did, project_did). Compared in full on mismatch drill-down.
COMPARE_COLS = [
    "project_status", "asset_did", "asset_id", "asset_name",
    "asset_requirement_count", "task_name", "task_name_clean", "task_status",
    "task_scheduled",
    "task_assigned_to_did", "task_assigned_to_collection",
    "task_assigned_to_name", "task_assigned_to_email",
    "task_submitted_on", "task_submitted_by_did", "task_submitted_by_name",
    "task_submitted_by_email",
    "task_approved_on", "task_approved_by_did", "task_approved_by_name",
    "task_approved_by_email",
    "task_cancelled_on", "task_cancelled_by_did", "task_cancelled_by_name",
    "task_cancelled_by_email",
]

HASH_SQL = """
SELECT project_did, count(*) AS rows,
       md5(string_agg(task_did || '|' || coalesce(task_status,'') || '|' ||
           coalesce(task_scheduled::text,'') || '|' || coalesce(task_name_clean,''),
           '|' ORDER BY task_did)) AS content_hash
FROM {table}
WHERE project_did = ANY($1::text[])
GROUP BY project_did
"""

ONLY_IN_SQL = """
SELECT task_did FROM (
  SELECT task_did FROM {a} WHERE project_did = $1
  EXCEPT
  SELECT task_did FROM {b} WHERE project_did = $1
) d ORDER BY task_did LIMIT 50
"""

# Doctrine rule 1: classify EVERY current-only row by whether its asset is
# visible to the walk at all. ghost = asset has zero INC rows.
MISSING_CLASSIFY_SQL = f"""
WITH missing AS (
  SELECT task_did FROM {CURRENT} WHERE project_did = $1
  EXCEPT
  SELECT task_did FROM {INC} WHERE project_did = $1
)
SELECT
  count(*) AS missing_total,
  count(*) FILTER (WHERE NOT asset_in_inc) AS ghost_rows,
  count(DISTINCT asset_did) FILTER (WHERE NOT asset_in_inc) AS ghost_assets,
  count(*) FILTER (WHERE asset_in_inc) AS unexplained_missing
FROM (
  SELECT c.asset_did,
         EXISTS (SELECT 1 FROM {INC} i
                 WHERE i.project_did = $1 AND i.asset_did = c.asset_did)
           AS asset_in_inc
  FROM {CURRENT} c JOIN missing m USING (task_did)
  WHERE c.project_did = $1
) x
"""

EXTRA_COUNT_SQL = f"""
SELECT count(*) AS extra_total FROM (
  SELECT task_did FROM {INC} WHERE project_did = $1
  EXCEPT
  SELECT task_did FROM {CURRENT} WHERE project_did = $1
) d
"""

# Doctrine rule 3: the only tolerated column-diff shape. All three *_by_did
# columns show the same export defect (dangling user DID that resolves to no
# name); the name-NULL-both-sides guard keeps this tolerance honest.
DANGLING_DID_COLS = {"task_submitted_by_did", "task_approved_by_did",
                     "task_cancelled_by_did"}
NAME_FOR_DID = {"task_submitted_by_did": "task_submitted_by_name",
                "task_approved_by_did": "task_approved_by_name",
                "task_cancelled_by_did": "task_cancelled_by_name"}

# Doctrine rule 4: bounded lifecycle lag between the hourly walk and the
# nightly export reload / weekly sweep.
LAG_CAP_DAYS = 7        # differing INC-side event dates may be this old
SWEEP_FRESH_DAYS = 8    # tolerance dies with the weekly sweep (7d + slack)

# Assignment/schedule edits bump only the task lastUpdated (asset-level
# pruning cannot see them): may lag in EITHER direction.
ASSIGNMENT_COLS = {"task_scheduled", "task_assigned_to_did",
                   "task_assigned_to_collection", "task_assigned_to_name",
                   "task_assigned_to_email"}
# Submit/approve/cancel touch the asset, so the walk sees them within one
# cycle: tolerated only when INC is AHEAD (current NULL -> inc value).
EVENT_COLS = {"task_submitted_on", "task_submitted_by_did",
              "task_submitted_by_name", "task_submitted_by_email",
              "task_approved_on", "task_approved_by_did",
              "task_approved_by_name", "task_approved_by_email",
              "task_cancelled_on", "task_cancelled_by_did",
              "task_cancelled_by_name", "task_cancelled_by_email"}
EVENT_DATE_COLS = {"task_submitted_on", "task_approved_on",
                   "task_cancelled_on"}
LIFECYCLE_COLS = {"task_status"} | ASSIGNMENT_COLS | EVENT_COLS
# Strictly-ahead check for task_status; terminal states share a rank so no
# terminal->terminal flip ever counts as "ahead".
STATUS_RANK = {"pending": 0, "in_progress": 1, "submitted": 2,
               "rejected": 3, "approved": 3, "cancelled": 3}

LAST_SWEEP_SQL = f"""
SELECT max(completed_at) AS last_sweep
FROM {SCHEMA_PIPELINE}.pipeline_runs
WHERE pipeline_name = '{PIPELINE_NAME}' AND status = 'success'
  AND metadata->>'mode' IN ('baseline', 'full-walk')
"""


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _is_lifecycle_lag(diff_cols, row, today):
    """Doctrine rule 4 for one shared-row diff. `row` carries c_<col> /
    i_<col> values; `diff_cols` is the differing subset of COMPARE_COLS."""
    if not diff_cols or not set(diff_cols) <= LIFECYCLE_COLS:
        return False
    for col in diff_cols:
        cur, inc = row[f"c_{col}"], row[f"i_{col}"]
        if col in ASSIGNMENT_COLS:
            continue
        if col == "task_status":
            if cur not in STATUS_RANK or inc not in STATUS_RANK:
                return False
            if STATUS_RANK[cur] >= STATUS_RANK[inc]:
                return False
        else:  # event column: fills in on INC only, and recently
            if cur is not None or inc is None:
                return False
            if col in EVENT_DATE_COLS and \
                    (today - _as_date(inc)).days > LAG_CAP_DAYS:
                return False
    return True


def _lag_tolerance_active(last_sweep_at, now):
    """Rule 4 is only honest while the weekly sweep bounds the lag."""
    if last_sweep_at is None:
        return False
    return now - last_sweep_at <= timedelta(days=SWEEP_FRESH_DAYS)


def _column_diffs(db, project_did, limit=20):
    """Full-column comparison for task_dids present on both sides. Returns
    {task_did: {col: [current_value, inc_value]}} for up to `limit` rows."""
    c_cols = ", ".join(f"c.{col} AS c_{col}" for col in COMPARE_COLS)
    i_cols = ", ".join(f"i.{col} AS i_{col}" for col in COMPARE_COLS)
    c_tuple = ", ".join(f"c.{col}" for col in COMPARE_COLS)
    i_tuple = ", ".join(f"i.{col}" for col in COMPARE_COLS)
    rows = db.fetch(
        f"SELECT c.task_did, {c_cols}, {i_cols} "
        f"FROM {CURRENT} c JOIN {INC} i USING (task_did) "
        f"WHERE c.project_did = $1 "
        f"AND ({c_tuple}) IS DISTINCT FROM ({i_tuple}) "
        f"ORDER BY c.task_did LIMIT {int(limit)}",
        project_did, timeout=AUDIT_QUERY_TIMEOUT,
    )
    diffs = {}
    for r in rows:
        cols = {}
        for col in COMPARE_COLS:
            cur, inc = r[f"c_{col}"], r[f"i_{col}"]
            if cur != inc:
                cols[col] = [str(cur) if cur is not None else None,
                             str(inc) if inc is not None else None]
        diffs[r["task_did"]] = cols
    return diffs


def _classify_column_diffs(db, project_did, today=None, lag_active=True):
    """Doctrine rules 3+4 over ALL shared-row diffs (no LIMIT). A diff row
    is tolerated as dangling-DID (rule 3, checked first) when every
    differing column is in DANGLING_DID_COLS and its paired *_by_name is
    NULL on BOTH sides; otherwise as lifecycle_lag (rule 4) when
    _is_lifecycle_lag holds and the sweep-freshness gate is on.
    Returns (dangling_rows, lifecycle_lag_rows, unexplained_rows,
    unexplained_sample)."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    c_cols = ", ".join(f"c.{col} AS c_{col}" for col in COMPARE_COLS)
    i_cols = ", ".join(f"i.{col} AS i_{col}" for col in COMPARE_COLS)
    c_tuple = ", ".join(f"c.{col}" for col in COMPARE_COLS)
    i_tuple = ", ".join(f"i.{col}" for col in COMPARE_COLS)
    rows = db.fetch(
        f"SELECT c.task_did, {c_cols}, {i_cols} "
        f"FROM {CURRENT} c JOIN {INC} i USING (task_did) "
        f"WHERE c.project_did = $1 "
        f"AND ({c_tuple}) IS DISTINCT FROM ({i_tuple})",
        project_did, timeout=AUDIT_QUERY_TIMEOUT,
    )
    dangling = 0
    lifecycle_lag = 0
    unexplained = 0
    unexplained_sample = {}
    for r in rows:
        diff_cols = [col for col in COMPARE_COLS
                     if r[f"c_{col}"] != r[f"i_{col}"]]
        is_dangling = bool(diff_cols) and all(
            col in DANGLING_DID_COLS
            and r[f"c_{NAME_FOR_DID[col]}"] is None
            and r[f"i_{NAME_FOR_DID[col]}"] is None
            for col in diff_cols
        )
        if is_dangling:
            dangling += 1
        elif lag_active and _is_lifecycle_lag(diff_cols, r, today):
            lifecycle_lag += 1
        else:
            unexplained += 1
            if len(unexplained_sample) < 20:
                unexplained_sample[r["task_did"]] = {
                    col: [str(r[f"c_{col}"]) if r[f"c_{col}"] is not None else None,
                          str(r[f"i_{col}"]) if r[f"i_{col}"] is not None else None]
                    for col in diff_cols}
    return dangling, lifecycle_lag, unexplained, unexplained_sample


def audit_project(db, project_did, current_side, inc_side, lag_active=True):
    """Compare one project's two sides under the doctrine. Returns the
    inc_audit_results row as a dict (without id/audited_at) plus a
    'doctrine_pass' key (not persisted as a column; encoded in notes)."""
    rows_current = current_side["rows"] if current_side else 0
    rows_inc = inc_side["rows"] if inc_side else 0
    hash_match = bool(
        current_side and inc_side
        and current_side["content_hash"] == inc_side["content_hash"]
    )
    result = {
        "project_did": project_did,
        "rows_current": rows_current,
        "rows_inc": rows_inc,
        "hash_match": hash_match,
        "missing_in_inc": None,
        "extra_in_inc": None,
        "column_diffs": None,
        "notes": None,
        "doctrine_pass": True,
    }
    if hash_match:
        result["notes"] = "DOCTRINE PASS (hash match)"
        return result

    # True counts under the doctrine (samples below stay LIMIT 50/20).
    m = db.fetch(MISSING_CLASSIFY_SQL, project_did,
                 timeout=AUDIT_QUERY_TIMEOUT)[0]
    extra_total = db.fetch(EXTRA_COUNT_SQL, project_did,
                           timeout=AUDIT_QUERY_TIMEOUT)[0]["extra_total"]
    dangling, lag_diffs, unexplained_diffs, unexplained_sample = \
        _classify_column_diffs(db, project_did, lag_active=lag_active)

    doctrine_pass = (m["unexplained_missing"] == 0 and unexplained_diffs == 0)

    missing_sample = [r["task_did"] for r in db.fetch(
        ONLY_IN_SQL.format(a=CURRENT, b=INC), project_did,
        timeout=AUDIT_QUERY_TIMEOUT)]
    extra_sample = [r["task_did"] for r in db.fetch(
        ONLY_IN_SQL.format(a=INC, b=CURRENT), project_did,
        timeout=AUDIT_QUERY_TIMEOUT)]
    # Persist the UNEXPLAINED diff sample when doctrine fails (that's what a
    # human must look at); otherwise keep the legacy any-diff sample.
    diffs_sample = unexplained_sample or _column_diffs(db, project_did)

    verdict = "PASS" if doctrine_pass else "FAIL"
    result.update(
        missing_in_inc=missing_sample or None,
        extra_in_inc=extra_sample or None,
        column_diffs=diffs_sample or None,
        doctrine_pass=doctrine_pass,
        notes=(f"DOCTRINE {verdict}: "
               f"missing={m['missing_total']} "
               f"(ghost={m['ghost_rows']} on {m['ghost_assets']} assets, "
               f"UNEXPLAINED={m['unexplained_missing']}); "
               f"adhoc_extra={extra_total}; "
               f"col_diffs(dangling_did={dangling}, "
               f"lifecycle_lag={lag_diffs}, "
               f"UNEXPLAINED={unexplained_diffs})"
               + ("" if lag_active else "; LAG TOLERANCE OFF (sweep stale)")
               + "; samples LIMIT 50/20"),
    )
    return result


def persist_result(db, result):
    db.execute(
        f"INSERT INTO {SCHEMA_PIPELINE}.inc_audit_results "
        f"(project_did, rows_current, rows_inc, hash_match, missing_in_inc, "
        f"extra_in_inc, column_diffs, notes) "
        f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        result["project_did"], result["rows_current"], result["rows_inc"],
        result["hash_match"], result["missing_in_inc"], result["extra_in_inc"],
        result["column_diffs"], result["notes"],
    )  # doctrine_pass is encoded in notes ("DOCTRINE PASS/FAIL: ..."), no schema change


def main():
    ap = argparse.ArgumentParser(
        description="Drift audit: stg_asset_tasks vs stg_asset_tasks_inc")
    ap.add_argument("--projects", default=PILOT_PROJECTS,
                    help=f"Comma list of project tails (default: "
                         f"{PILOT_PROJECTS}), or 'all13'")
    args = ap.parse_args()

    setup_logging()
    db = get_tx_db()
    projects = resolve_projects(db, args.projects)
    dids = [p["project_did"] for p in projects]
    tail_by_did = {p["project_did"]: _project_tail(p["project_name"])
                   for p in projects}

    current_rows = {r["project_did"]: r for r in db.fetch(
        HASH_SQL.format(table=CURRENT), dids, timeout=AUDIT_QUERY_TIMEOUT)}
    inc_rows = {r["project_did"]: r for r in db.fetch(
        HASH_SQL.format(table=INC), dids, timeout=AUDIT_QUERY_TIMEOUT)}

    logger.info("CAVEAT: the two pipelines run at different times, so small "
                "drift right after either run is expected; persistent "
                "same-direction drift is the bug signal.")

    last_sweep = db.fetch(LAST_SWEEP_SQL,
                          timeout=AUDIT_QUERY_TIMEOUT)[0]["last_sweep"]
    lag_active = _lag_tolerance_active(last_sweep, datetime.now(timezone.utc))
    if lag_active:
        logger.info(f"Rule 4 lifecycle-lag tolerance ACTIVE "
                    f"(last sweep {last_sweep:%Y-%m-%d %H:%M} UTC)")
    else:
        logger.warning(
            f"Rule 4 lifecycle-lag tolerance OFF — last successful "
            f"baseline/full-walk is {last_sweep or 'MISSING'} (> "
            f"{SWEEP_FRESH_DAYS} days): the weekly sweep is not bounding "
            f"the lag; fix the Saturday dispatch before trusting FAILs")

    any_fail = False
    for did in dids:
        tail = tail_by_did[did]
        result = audit_project(db, did, current_rows.get(did), inc_rows.get(did),
                               lag_active=lag_active)
        retry_tx_db(lambda r=result: persist_result(db, r),
                    description=f"insert inc_audit_results [{tail}]")
        if result["doctrine_pass"]:
            logger.info(f"[{tail}] {result['notes']} "
                        f"(rows {result['rows_current']} vs {result['rows_inc']})")
            continue
        any_fail = True
        logger.warning(
            f"[{tail}] {result['notes']} "
            f"(rows_current={result['rows_current']} rows_inc={result['rows_inc']})"
        )
        if result["missing_in_inc"]:
            logger.warning(f"[{tail}]   missing_in_inc sample: "
                           f"{result['missing_in_inc'][:10]}")
        if result["column_diffs"]:
            logger.warning(f"[{tail}]   UNEXPLAINED column diffs sample: "
                           f"{json.dumps(result['column_diffs'], default=str)[:2000]}")

    close_tx_db()
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
