#!/usr/bin/env python3
"""Drift audit: stg_asset_tasks (current pipeline, authoritative) vs
stg_asset_tasks_inc (incremental shadow pilot).

Per project: row counts + an order-independent content hash over the audit
columns (task_did, task_status, task_scheduled, task_name_clean). On any
mismatch, drills down: task_dids present on only one side (LIMIT 50 each
direction) and full-column diffs for shared task_dids (LIMIT 20; at pilot
scale every shared business column is compared, not just the hash columns).

EVERY run inserts one row per project into pipeline.inc_audit_results, pass
or fail; the test week's expand/fix/abort decision is made from that table.

Exit 0 when every project matches, 1 otherwise.

CAVEAT (also printed on every run): the two pipelines run at different
times, so small drift right after either run is expected and self-corrects
by the next audit. Persistent same-direction drift is the bug signal.

See docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md
(Task 7) for context.
"""
import argparse
import json
import sys

from config import SCHEMA_PIPELINE, SCHEMA_STAGING, setup_logging, get_logger
from db_tx import close_tx_db, get_tx_db, retry_tx_db
from extract_asset_tasks_inc import PILOT_PROJECTS, _project_tail, resolve_projects

logger = get_logger("audit_asset_tasks_inc")

CURRENT = f"{SCHEMA_STAGING}.stg_asset_tasks"
INC = f"{SCHEMA_STAGING}.stg_asset_tasks_inc"

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
        project_did,
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


def audit_project(db, project_did, current_side, inc_side):
    """Compare one project's two sides (rows from HASH_SQL, possibly None
    when a side has no rows at all). Returns the inc_audit_results row as a
    dict (without id/audited_at)."""
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
    }
    if hash_match:
        return result

    missing = [r["task_did"] for r in db.fetch(
        ONLY_IN_SQL.format(a=CURRENT, b=INC), project_did)]
    extra = [r["task_did"] for r in db.fetch(
        ONLY_IN_SQL.format(a=INC, b=CURRENT), project_did)]
    diffs = _column_diffs(db, project_did)
    result.update(
        missing_in_inc=missing or None,
        extra_in_inc=extra or None,
        column_diffs=diffs or None,
        notes=(f"{len(missing)} missing / {len(extra)} extra shown (LIMIT 50 "
               f"each); {len(diffs)} column-diff rows shown (LIMIT 20)"),
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
    )


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
        HASH_SQL.format(table=CURRENT), dids)}
    inc_rows = {r["project_did"]: r for r in db.fetch(
        HASH_SQL.format(table=INC), dids)}

    logger.info("CAVEAT: the two pipelines run at different times, so small "
                "drift right after either run is expected; persistent "
                "same-direction drift is the bug signal.")

    any_mismatch = False
    for did in dids:
        tail = tail_by_did[did]
        result = audit_project(db, did, current_rows.get(did), inc_rows.get(did))
        retry_tx_db(lambda r=result: persist_result(db, r),
                    description=f"insert inc_audit_results [{tail}]")
        if result["hash_match"]:
            logger.info(f"[{tail}] MATCH: {result['rows_current']} rows both sides")
            continue
        any_mismatch = True
        logger.warning(
            f"[{tail}] MISMATCH: rows_current={result['rows_current']} "
            f"rows_inc={result['rows_inc']}; "
            f"missing_in_inc={len(result['missing_in_inc'] or [])} "
            f"extra_in_inc={len(result['extra_in_inc'] or [])} (LIMIT 50 each)"
        )
        if result["missing_in_inc"]:
            logger.warning(f"[{tail}]   missing_in_inc sample: "
                           f"{result['missing_in_inc'][:10]}")
        if result["extra_in_inc"]:
            logger.warning(f"[{tail}]   extra_in_inc sample: "
                           f"{result['extra_in_inc'][:10]}")
        if result["column_diffs"]:
            logger.warning(f"[{tail}]   column diffs (LIMIT 20): "
                           f"{json.dumps(result['column_diffs'], default=str)[:2000]}")

    close_tx_db()
    sys.exit(1 if any_mismatch else 0)


if __name__ == "__main__":
    main()
