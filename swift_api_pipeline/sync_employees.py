"""Sync employee reference data from Google Sheet to Supabase.

Reads the Google Sheet, compares with reference.ref_employees,
and upserts changes. Handles new employees, updates, and resignations
with history tracking (new row per change with effective_date).

Usage:
    python sync_employees.py                  # dry run
    python sync_employees.py --apply          # apply changes to Supabase
    python sync_employees.py --apply --date 2026-04-08  # with specific effective date
"""

import argparse
import csv
import io
import sys
from datetime import date, datetime
from config import get_logger, get_db, close_db, retry_db, setup_logging
from sheets_client import authenticate_sheets, read_spreadsheet

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

setup_logging()
logger = get_logger("sync_employees")

# Google Sheet ID — Ms. Orv's (HR) authoritative "Active Employee Information" roster.
# This sheet lists ACTIVE employees only and uses human-friendly headers (mapped below).
SHEET_ID = "1zYiSJ5dERJaFMCto9saTg_O76BFOl75adUkubt4qSFk"

# Map the HR sheet's headers (lowercased) -> ref_employees columns.
# Headers not listed here are ignored (No, Tenure, Tenure Bracket, Role Tagging).
HEADER_MAP = {
    "id number": "emp_id",
    "last name": "last_name",
    "first name": "first_name",
    "middle name": "middle_name",
    "full name": "full_name",
    "nickname": "nickname",
    "email address": "email",
    "start date": "hire_date",
    "date regularized": "regularization_date",
    "position": "position",
    "work schedule": "work_schedule",
    "employment status": "employment_status",
    "carrier": "carrier_group",          # e.g. "CG1 - Verizon" -> carrier_group
    "division": "division",
    "sub division": "sub_division",
    "time in (pht)": "shift_time_in_pht",
    "time out (pht)": "shift_time_out_pht",
    "immediate supervisor": "immediate_supervisor",
}
DATE_FIELDS = ("hire_date", "regularization_date")
# Derive the short carrier from carrier_group for new-employee inserts.
CARRIER_FROM_GROUP = {"CG1 - Verizon": "Verizon", "CG2 - AT&T/DISH": "AT&T/DISH",
                      "CG3 - TMO/USCC": "TMO/USCC"}
# emp_ids whose email must NOT be overwritten from the sheet (sheet is stale for them).
EMAIL_LOCK = {"190503"}  # Ronald Figueroa: keep ronald@ (HR sheet still has old ton@)
# Safety: skip resignation-on-absence if the sheet read returns implausibly few rows
# (guards against a partial/failed read wrongly inactivating everyone).
RESIGNATION_MIN_SHEET_FRACTION = 0.5

# Columns that trigger a new history row when changed
TRACKED_FIELDS = {"role2", "cluster", "carrier", "carrier_group", "division",
                  "sub_division", "position", "is_active", "resignation_date",
                  "regularization_date",
                  "work_schedule", "shift_schedule", "employment_status"}

# Columns that update in place (no history needed)
SIMPLE_FIELDS = {"last_name", "first_name", "middle_name", "full_name",
                 "nickname", "email", "hire_date",
                 "immediate_supervisor", "shift_time_in_pht", "shift_time_out_pht"}


def _parse_date(val):
    val = (val or "").strip()
    if not val or val in ("None", "FALSE", "TRUE", "-", "#VALUE!"):
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue
    return None


def read_sheet():
    """Read active employees from Ms. Orv's authoritative HR roster.

    The sheet has title/blank rows above a human-friendly header row and lists
    ACTIVE employees only, so we locate the header by finding the row containing an
    'ID Number' cell and translate columns via HEADER_MAP. Every employee present is
    treated as active; departures are handled as resignations by the caller (absent
    from this sheet -> inactive).
    """
    creds = authenticate_sheets()
    rows = read_spreadsheet(creds, SHEET_ID)
    if not rows:
        logger.warning("Sheet is empty")
        return []

    header_idx = None
    for i, row in enumerate(rows):
        if any(str(c).strip().lower() == "id number" for c in row):
            header_idx = i
            break
    if header_idx is None:
        logger.error("Could not find header row (no 'ID Number' column); aborting read")
        return []

    headers = [str(h).strip().lower() for h in rows[header_idx]]
    employees = []
    for row in rows[header_idx + 1:]:
        rec = {}
        for i, h in enumerate(headers):
            col = HEADER_MAP.get(h)
            if not col:
                continue
            # Collapse stray tabs/newlines/repeated spaces (the sheet has e.g. a tab
            # inside "Kyla\tGueriba Palo") to single spaces, and strip.
            rec[col] = " ".join(row[i].split()) if i < len(row) and row[i] else ""
        emp_id = rec.get("emp_id", "").strip()
        if not emp_id:
            continue  # spacer / total rows
        # Work schedule: only trust valid values (sheet occasionally has FALSE/#VALUE!).
        if rec.get("work_schedule") not in ("4DWW", "5DWW"):
            rec["work_schedule"] = ""
        # Drop spreadsheet error/junk values from free-text fields.
        for f in ("shift_time_in_pht", "shift_time_out_pht", "immediate_supervisor"):
            if rec.get(f) in ("#VALUE!", "FALSE", "-"):
                rec[f] = ""
        for f in DATE_FIELDS:
            rec[f] = _parse_date(rec.get(f))
        rec["resignation_date"] = None
        rec["is_active"] = True  # active-only sheet
        rec["carrier"] = CARRIER_FROM_GROUP.get(rec.get("carrier_group", ""),
                                                rec.get("carrier", ""))
        employees.append(rec)

    logger.info(f"Read {len(employees)} active employees from HR roster (header row {header_idx})")
    return employees


def get_current_employees(db):
    """Get latest state of each employee from ref_employees."""
    rows = retry_db(
        lambda: db.fetch(
            "SELECT DISTINCT ON (emp_id) * FROM reference.ref_employees "
            "ORDER BY emp_id, effective_date DESC"
        ),
        description="get current employees",
    )
    return {str(r["emp_id"]): dict(r) for r in rows}


def sync(db, sheet_employees, effective_date, apply=False):
    """Compare sheet with DB and generate changes."""
    current = get_current_employees(db)
    logger.info(f"Current DB employees: {len(current)}")

    new_employees = []
    updated_employees = []
    resigned_employees = []

    for emp in sheet_employees:
        emp_id = emp["emp_id"]
        existing = current.get(emp_id)

        if not existing:
            # New employee
            new_employees.append(emp)
            continue

        # Check for tracked field changes (need new history row)
        tracked_changes = {}
        for field in TRACKED_FIELDS:
            sheet_val = emp.get(field)
            db_val = existing.get(field)
            # Normalize for comparison
            s_str = str(sheet_val).strip() if sheet_val not in (None, "", "None") else ""
            d_str = str(db_val).strip() if db_val not in (None, "", "None") else ""
            if isinstance(db_val, bool):
                d_str = str(db_val)
                s_str = str(sheet_val)
            if s_str != d_str and s_str:  # only flag if sheet has a value
                tracked_changes[field] = (db_val, sheet_val)

        # Check for simple field changes (update in place)
        simple_changes = {}
        for field in SIMPLE_FIELDS:
            sheet_val = emp.get(field, "")
            db_val = existing.get(field)
            s_str = str(sheet_val).strip() if sheet_val not in (None, "", "None") else ""
            d_str = str(db_val).strip() if db_val not in (None, "", "None") else ""
            if field == "email":
                # Never revert locked emails; compare case-insensitively to avoid churn.
                if emp_id in EMAIL_LOCK or s_str.lower() == d_str.lower():
                    continue
            # Skip encoding corruption (ñ -> Ã±, etc.)
            if "Ã" in s_str and "ñ" in d_str:
                continue
            if s_str != d_str and s_str:  # only flag if sheet has a value
                simple_changes[field] = (db_val, sheet_val)

        if tracked_changes or simple_changes:
            updated_employees.append({
                "emp": emp,
                "existing": existing,
                "tracked_changes": tracked_changes,
                "simple_changes": simple_changes,
            })

    # Employees active in DB but absent from the authoritative active-only HR roster
    # are treated as departures (set inactive). HR's roster is the source of truth.
    sheet_ids = {e["emp_id"] for e in sheet_employees}
    for emp_id, existing in current.items():
        if emp_id not in sheet_ids and existing.get("is_active"):
            resigned_employees.append(existing)

    # Last day for a departure = their last APPROVED work date (true final working day),
    # even if HR removed them from the roster earlier. Computed once, used for both the
    # report and the resignation_date written on --apply.
    resign_last_day = {}
    if resigned_employees:
        _rids = [e["emp_id"] for e in resigned_employees]
        _ld = retry_db(
            lambda: db.fetch(
                "SELECT emp_id, max(work_date) AS last_day FROM data_staging.stg_daily_reports "
                "WHERE emp_id = ANY($1) AND approved_on IS NOT NULL GROUP BY emp_id",
                _rids,
            ),
            description="last approved work date for departures",
        ) or []
        resign_last_day = {str(r["emp_id"]): r["last_day"] for r in _ld}

    # Report
    print(f"\n=== Sync Summary ===")
    print(f"  New employees: {len(new_employees)}")
    print(f"  Updated employees: {len(updated_employees)}")
    print(f"  Departures (inactivate): {len(resigned_employees)}")

    if new_employees:
        print(f"\n--- New Employees ---")
        for e in new_employees:
            print(f"  {e['emp_id']} | {e.get('full_name', '')} | {e.get('position', '')} | {e.get('email', '')}")

    if updated_employees:
        print(f"\n--- Updates ---")
        for u in updated_employees:
            emp = u["emp"]
            print(f"  {emp['emp_id']} | {emp.get('full_name', '')}")
            for field, (old, new) in {**u["tracked_changes"], **u["simple_changes"]}.items():
                print(f"    {field}: {old} -> {new}")

    if resigned_employees:
        print(f"\n--- Departures (active in DB, absent from HR active roster) ---")
        print(f"    These will be set is_active=false on --apply; resignation_date = last approved work date:")
        for e in resigned_employees:
            ld = resign_last_day.get(e["emp_id"])
            print(f"  {e['emp_id']} | {e.get('full_name', '')} | {e.get('email', '')} | "
                  f"last day: {ld or 'unknown (no approved reports)'}")

    summary = {"new": new_employees, "updated": updated_employees,
               "resigned": resigned_employees}

    if not apply:
        print(f"\n=== DRY RUN (use --apply to execute) ===")
        return summary

    # Apply changes
    applied = 0

    # New employees
    for emp in new_employees:
        retry_db(
            lambda e=emp: db.execute(
                "INSERT INTO reference.ref_employees "
                "(emp_id, last_name, first_name, middle_name, full_name, nickname, email, "
                " position, role2, carrier, carrier_group, cluster, division, sub_division, "
                " work_schedule, shift_schedule, employment_status, hire_date, is_active, "
                " resignation_date, regularization_date, immediate_supervisor, "
                " shift_time_in_pht, shift_time_out_pht, effective_date, change_reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,"
                "$21,$22,$23,$24,$25,$26) "
                "ON CONFLICT (emp_id, effective_date) DO NOTHING",
                e.get("emp_id"), e.get("last_name"), e.get("first_name"), e.get("middle_name"),
                e.get("full_name"), e.get("nickname"), e.get("email"),
                e.get("position"), e.get("role2"), e.get("carrier"), e.get("carrier_group"),
                e.get("cluster"), e.get("division"), e.get("sub_division"),
                e.get("work_schedule"), e.get("shift_schedule"), e.get("employment_status"),
                e.get("hire_date"), e.get("is_active", True), e.get("resignation_date"),
                e.get("regularization_date"), e.get("immediate_supervisor"),
                e.get("shift_time_in_pht"), e.get("shift_time_out_pht"),
                e.get("hire_date") or effective_date, "New employee from Google Sheet sync",
            ),
            description=f"insert {emp.get('full_name', '')}",
        )
        applied += 1

    # Updates — reference.ref_employees is one row per emp_id (UNIQUE(emp_id)), so we
    # update the existing row in place rather than inserting effective-dated history
    # rows (which the UNIQUE(emp_id) constraint forbids). Tracked + simple changes are
    # applied together in a single UPDATE.
    for u in updated_employees:
        emp = u["emp"]
        changed = {**u["tracked_changes"], **u["simple_changes"]}
        if not changed:
            continue
        cols = list(changed.keys())
        set_sql = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        reason = "Sheet sync: " + ", ".join(f"{c}: {o}->{n}" for c, (o, n) in changed.items())
        params = [emp.get(c) for c in cols] + [reason, emp["emp_id"]]
        n = len(cols)
        sql = (f"UPDATE reference.ref_employees SET {set_sql}, "
               f"change_reason = ${n + 1}, updated_at = NOW() WHERE emp_id = ${n + 2}")
        retry_db(
            lambda s=sql, p=params: db.execute(s, *p),
            description=f"update {emp.get('full_name', '')}",
        )
        applied += 1

    # Resignations / departures: active in DB but absent from the active-only HR roster.
    # Guard: skip if the sheet returned implausibly few rows (likely a partial/failed read),
    # so a bad read can't mass-inactivate the roster.
    active_db = sum(1 for e in current.values() if e.get("is_active"))
    if resigned_employees and len(sheet_employees) < RESIGNATION_MIN_SHEET_FRACTION * max(active_db, 1):
        print(f"\n[!] SKIPPING {len(resigned_employees)} resignation(s): HR sheet returned only "
              f"{len(sheet_employees)} rows vs {active_db} active in DB — likely a partial read.")
        logger.warning("Resignation apply skipped: sheet row count below safety threshold")
    else:
        for e in resigned_employees:
            # Last day = last approved work date (see resign_last_day above); fall back to
            # any existing resignation_date if they have no approved reports.
            resign_dt = resign_last_day.get(e["emp_id"]) or e.get("resignation_date")
            reason = (f"Inactivated: removed from HR active roster; last approved work date "
                      f"{resign_dt}" if resign_dt else
                      "Inactivated: removed from HR active roster (no approved reports)")
            retry_db(
                lambda eid=e["emp_id"], rd=resign_dt, rsn=reason: db.execute(
                    "UPDATE reference.ref_employees SET is_active = false, "
                    "resignation_date = $1, change_reason = $2, updated_at = NOW() "
                    "WHERE emp_id = $3",
                    rd, rsn, eid,
                ),
                description=f"inactivate {e.get('full_name', '')} (last day {resign_dt})",
            )
            applied += 1

    print(f"\nApplied {applied} changes")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes to Supabase")
    parser.add_argument("--date", type=str, help="Effective date for changes (YYYY-MM-DD)")
    args = parser.parse_args()

    if not SHEET_ID:
        print("ERROR: SHEET_ID not set. Create the Google Sheet, then update SHEET_ID in this script.")
        return

    effective_date = date.fromisoformat(args.date) if args.date else date.today()

    sheet_employees = read_sheet()
    if not sheet_employees:
        return

    db = get_db()
    sync(db, sheet_employees, effective_date, apply=args.apply)
    close_db()


if __name__ == "__main__":
    main()
