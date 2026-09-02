"""Sync the HR schedule-changes Google Sheet into OntelDB.

Flow (spec: ai-projects/docs/superpowers/specs/2026-09-02-schedule-change-history.md):
  fetch all tabs (Sheets v4) -> parse template tabs (schedule_changes_source)
  -> resolve emp_ids against reference.ref_employees -> wipe guard
  -> full-replace data_raw.raw_schedule_changes and rebuild
  data_staging.stg_schedule_change_history -> pipeline.pipeline_runs bookkeeping.

Each table is refreshed by upsert-on-PK then prune-by-run-id (two statements).
PipelineDB acquires a fresh pooled connection per call, so a cross-call
BEGIN/COMMIT cannot span them; upsert+prune never leaves the table empty and a
run that dies between the two only leaves stale extras that the next run prunes.

Usage:
  SCHEDULE_SHEETS_TOKEN=<sheets-scoped token pickle> python sync_schedule_changes.py [--dry-run]

Exit codes: 0 ok, 1 failed, 2 aborted by the wipe guard.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone

from schedule_changes_source import (
    SCHEDULE_SHEET_ID,
    ParsedRow,
    fetch_all_tabs,
    find_template_header,
    load_sheets_creds,
    parse_tab,
)

PIPELINE_NAME = "schedule_changes_sync"

# Wipe guard: the sheet holds ~700 rows (2026-09-02); a partial read must never
# replace a full snapshot. Small previous counts stand down (first loads).
GUARD_MIN_PREV = 20
GUARD_MIN_FRACTION = 0.5

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

PAYLOAD_KEYS = [
    "id_number", "names", "role", "shift_start_pht", "shift_start_et",
    "shift_end_pht", "shift_end_et", "rest_day", "work_arrangement",
    "reg_hours", "shift", "start_date", "end_date", "month", "year",
    "rdo_to", "day", "notes",
]

# Full-replace is upsert-then-prune, NOT a single DELETE+INSERT CTE statement:
# data-modifying CTEs share one snapshot, so the INSERT's unique check cannot
# see the DELETE's work and re-runs hit duplicate-key errors. Upsert + prune
# never leaves the table empty and self-heals if a run dies between the two.
#
# ($1::text)::jsonb, not $1::jsonb: with a bare jsonb cast asyncpg infers the
# parameter type as jsonb and encodes the Python str as a JSON string SCALAR
# (jsonb_typeof = 'string'), so jsonb_to_recordset sees a non-array. Forcing
# text first makes Postgres parse the JSON itself.
_RAW_UPSERT_SQL = """
WITH src AS (SELECT * FROM jsonb_to_recordset(($1::text)::jsonb)
             AS x(sheet_tab text, row_index int, payload jsonb, row_hash text))
INSERT INTO data_raw.raw_schedule_changes
    (sheet_tab, row_index, payload, row_hash, load_run_id)
SELECT sheet_tab, row_index, payload, row_hash, $2::uuid FROM src
ON CONFLICT (sheet_tab, row_index) DO UPDATE SET
    payload = EXCLUDED.payload,
    row_hash = EXCLUDED.row_hash,
    load_run_id = EXCLUDED.load_run_id,
    extracted_at = now()
"""

_RAW_PRUNE_SQL = """
DELETE FROM data_raw.raw_schedule_changes WHERE load_run_id <> $1::uuid
"""

_STAGING_UPSERT_SQL = """
WITH src AS (SELECT * FROM jsonb_to_recordset(($1::text)::jsonb)
             AS x(emp_id text, member_name text, role text, sheet_tab text,
                  shift_start_pht text, shift_end_pht text,
                  shift_start_et text, shift_end_et text,
                  shift_code text, work_arrangement text, reg_hours int,
                  rest_day text, rdo_to date, rdo_day text,
                  start_date date, end_date date, change_kind text,
                  notes text, row_hash text))
INSERT INTO data_staging.stg_schedule_change_history
    (emp_id, member_name, role, sheet_tab, shift_start_pht, shift_end_pht,
     shift_start_et, shift_end_et, shift_code, work_arrangement, reg_hours,
     rest_day, rdo_to, rdo_day, start_date, end_date, change_kind, notes,
     row_hash, load_run_id)
-- COALESCE: shift_start_pht is part of the PK; a handful of sheet rows have a
-- blank PHT start cell ('' sorts first and displays as "not recorded").
SELECT emp_id, member_name, role, sheet_tab, COALESCE(shift_start_pht, ''), shift_end_pht,
       shift_start_et, shift_end_et, shift_code, work_arrangement, reg_hours,
       rest_day, rdo_to, rdo_day, start_date, end_date, change_kind, notes,
       row_hash, $2::uuid
FROM src
ON CONFLICT (emp_id, sheet_tab, start_date, shift_start_pht) DO UPDATE SET
    member_name = EXCLUDED.member_name,
    role = EXCLUDED.role,
    shift_end_pht = EXCLUDED.shift_end_pht,
    shift_start_et = EXCLUDED.shift_start_et,
    shift_end_et = EXCLUDED.shift_end_et,
    shift_code = EXCLUDED.shift_code,
    work_arrangement = EXCLUDED.work_arrangement,
    reg_hours = EXCLUDED.reg_hours,
    rest_day = EXCLUDED.rest_day,
    rdo_to = EXCLUDED.rdo_to,
    rdo_day = EXCLUDED.rdo_day,
    end_date = EXCLUDED.end_date,
    change_kind = EXCLUDED.change_kind,
    notes = EXCLUDED.notes,
    row_hash = EXCLUDED.row_hash,
    load_run_id = EXCLUDED.load_run_id,
    extracted_at = now()
"""

_STAGING_PRUNE_SQL = """
DELETE FROM data_staging.stg_schedule_change_history WHERE load_run_id <> $1::uuid
"""


def guard_says_abort(prev_count: int, new_count: int) -> bool:
    return prev_count >= GUARD_MIN_PREV and new_count < GUARD_MIN_FRACTION * prev_count


def _name_tokens(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", str(name or "").casefold())
    return [t for t in cleaned.split() if t and t not in NAME_SUFFIXES]


def resolve_emp_ids(db, rows: list[ParsedRow]) -> tuple[list[tuple[str, ParsedRow]], list[str]]:
    """Resolve each parsed row to an emp_id.

    * id_number present -> used as emp_id even when the roster misses it (the
      sheet ID Number IS the ref_employees emp_id keyspace; old members are
      absent from the roster). Unknown ids are noted to stderr.
    * blank id_number -> unique first+last name-token match against the latest
      ref_employees row per emp_id (generational suffixes stripped);
      ambiguous or unmatched rows are skipped with a reason.
    """
    roster = db.fetch(
        "SELECT DISTINCT ON (emp_id) emp_id, full_name, first_name, nickname "
        "FROM reference.ref_employees "
        "ORDER BY emp_id, effective_date DESC NULLS LAST")
    known_ids = {str(r["emp_id"]) for r in roster}
    index: dict[tuple[str, str], set[str]] = {}
    for r in roster:
        full = _name_tokens(r["full_name"])
        if len(full) >= 2:
            index.setdefault((full[0], full[-1]), set()).add(str(r["emp_id"]))
            for alias in (r["first_name"], r["nickname"]):
                alias_toks = _name_tokens(alias or "")
                if alias_toks:
                    index.setdefault((alias_toks[0], full[-1]), set()).add(str(r["emp_id"]))

    resolved: list[tuple[str, ParsedRow]] = []
    skips: list[str] = []
    for row in rows:
        if row.id_number:
            if row.id_number not in known_ids:
                print(f"  NOTE: {row.sheet_tab}!r{row.row_index}: unknown_emp_id "
                      f"{row.id_number} ({row.member_name}); kept", file=sys.stderr)
            resolved.append((row.id_number, row))
            continue
        toks = _name_tokens(row.member_name)
        if len(toks) < 2:
            skips.append(f"{row.sheet_tab}!r{row.row_index}: unmatched name "
                         f"{row.member_name!r} (too short)")
            continue
        hits = index.get((toks[0], toks[-1]), set())
        if len(hits) == 1:
            resolved.append((next(iter(hits)), row))
        elif len(hits) > 1:
            skips.append(f"{row.sheet_tab}!r{row.row_index}: ambiguous name "
                         f"{row.member_name!r} -> {sorted(hits)}")
        else:
            skips.append(f"{row.sheet_tab}!r{row.row_index}: unmatched name "
                         f"{row.member_name!r}")
    return resolved, skips


def _payload(row: ParsedRow) -> dict[str, str]:
    cells = list(row.raw_cells)
    payload = {key: (cells[i] if i < len(cells) else "")
               for i, key in enumerate(PAYLOAD_KEYS)}
    extra = [str(c) for c in cells[len(PAYLOAD_KEYS):] if str(c).strip()]
    if extra:
        payload["extra"] = " | ".join(extra)
    return payload


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def run(dry_run: bool) -> int:
    from db import get_db, retry_db  # deferred: keeps parser tests DB-free

    creds = load_sheets_creds()
    tabs = fetch_all_tabs(SCHEDULE_SHEET_ID, creds)

    parsed: list[ParsedRow] = []
    parse_skips: list[str] = []
    skipped_tabs: list[str] = []
    for title, grid in tabs:
        if find_template_header(grid) < 0:
            skipped_tabs.append(title)
            continue
        rows, skips = parse_tab(title, grid)
        parsed.extend(rows)
        parse_skips.extend(skips)

    db = get_db()
    resolved, resolve_skips = resolve_emp_ids(db, parsed)

    # Dedupe on the staging PK: keep the first occurrence (sheet order).
    seen: set[tuple] = set()
    staged: list[tuple[str, ParsedRow]] = []
    dupe_skips: list[str] = []
    for emp_id, row in resolved:
        # "or ''" matches the COALESCE in _STAGING_SWAP_SQL (PK-safe null).
        key = (emp_id, row.sheet_tab, row.start_date, row.shift_start_pht or "")
        if key in seen:
            dupe_skips.append(f"{row.sheet_tab}!r{row.row_index}: duplicate_in_sheet "
                              f"{emp_id} {row.start_date} {row.shift_start_pht}")
            continue
        seen.add(key)
        staged.append((emp_id, row))

    prev = retry_db(
        lambda: db.fetchval("SELECT COUNT(*) FROM data_staging.stg_schedule_change_history"),
        description="previous staging count")

    print(f"Tabs: {len(tabs)} total, {len(tabs) - len(skipped_tabs)} template, "
          f"skipped: {', '.join(skipped_tabs) or '(none)'}")
    print(f"Rows: {len(parsed)} parsed raw, {len(staged)} staged "
          f"({len(resolve_skips)} unresolved, {len(dupe_skips)} in-sheet dupes, "
          f"{len(parse_skips)} parse skips); previous staging count {prev}")
    for line in parse_skips + resolve_skips + dupe_skips:
        print(f"  SKIP: {line}")

    if guard_says_abort(prev, len(staged)):
        msg = (f"wipe guard: staged {len(staged)} < {GUARD_MIN_FRACTION:.0%} of "
               f"previous {prev}; aborting before any write")
        print(f"ABORT: {msg}", file=sys.stderr)
        if not dry_run:
            _record_run(db, retry_db, status="failed", records=0, error=msg)
        return 2

    if dry_run:
        print("Dry run: no writes.")
        return 0

    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    retry_db(
        lambda: db.execute(
            "INSERT INTO pipeline.pipeline_runs "
            "(run_id, pipeline_name, status, started_at) VALUES ($1, $2, $3, $4)",
            run_id, PIPELINE_NAME, "running", started),
        description="insert pipeline_runs")

    try:
        # Every parsed row lands raw, resolved or not (identity work can
        # happen later against the raw snapshot).
        raw_json = json.dumps([
            {"sheet_tab": r.sheet_tab, "row_index": r.row_index,
             "payload": _payload(r), "row_hash": r.row_hash}
            for r in parsed
        ])
        retry_db(
            lambda: db.execute(_RAW_UPSERT_SQL, raw_json, run_id),
            description="raw upsert")
        retry_db(
            lambda: db.execute(_RAW_PRUNE_SQL, run_id),
            description="raw prune")

        staging_json = json.dumps([
            {"emp_id": emp_id, "member_name": r.member_name, "role": r.role,
             "sheet_tab": r.sheet_tab,
             "shift_start_pht": r.shift_start_pht, "shift_end_pht": r.shift_end_pht,
             "shift_start_et": r.shift_start_et, "shift_end_et": r.shift_end_et,
             "shift_code": r.shift_code, "work_arrangement": r.work_arrangement,
             "reg_hours": r.reg_hours, "rest_day": r.rest_day,
             "rdo_to": _iso(r.rdo_to), "rdo_day": r.rdo_day,
             "start_date": _iso(r.start_date), "end_date": _iso(r.end_date),
             "change_kind": r.change_kind, "notes": r.notes, "row_hash": r.row_hash}
            for emp_id, r in staged
        ])
        retry_db(
            lambda: db.execute(_STAGING_UPSERT_SQL, staging_json, run_id),
            description="staging upsert")
        retry_db(
            lambda: db.execute(_STAGING_PRUNE_SQL, run_id),
            description="staging prune")
    except Exception as err:
        _record_run(db, retry_db, status="failed", records=0, error=str(err),
                    run_id=run_id)
        raise

    _record_run(db, retry_db, status="success", records=len(staged), error=None,
                run_id=run_id)
    print(f"Done: {len(parsed)} raw rows, {len(staged)} staged rows (run {run_id}).")
    return 0


def _record_run(db, retry_db, status: str, records: int, error: str | None,
                run_id: str | None = None) -> None:
    if run_id is None:
        run_id = str(uuid.uuid4())
        retry_db(
            lambda: db.execute(
                "INSERT INTO pipeline.pipeline_runs "
                "(run_id, pipeline_name, status, started_at) VALUES ($1, $2, $3, $4)",
                run_id, PIPELINE_NAME, "running", datetime.now(timezone.utc)),
            description="insert pipeline_runs")
    retry_db(
        lambda: db.execute(
            "UPDATE pipeline.pipeline_runs SET status = $1, completed_at = $2, "
            "records_extracted = $3, error_message = $4 WHERE run_id = $5",
            status, datetime.now(timezone.utc), records, error, run_id),
        description="update pipeline_runs")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + parse + resolve + summary; no DB writes")
    args = ap.parse_args()
    try:
        return run(dry_run=args.dry_run)
    except Exception as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
