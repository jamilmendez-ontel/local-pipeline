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

# Google Sheet ID — update after creating the sheet
SHEET_ID = "140MFMHPi-c6QARjQ0d2z1seethtEpnAPDNG5tIL9-qc"

# Columns that trigger a new history row when changed
TRACKED_FIELDS = {"role2", "cluster", "carrier", "carrier_group", "division",
                  "sub_division", "position", "is_active", "resignation_date",
                  "work_schedule", "shift_schedule", "employment_status"}

# Columns that update in place (no history needed)
SIMPLE_FIELDS = {"last_name", "first_name", "middle_name", "full_name",
                 "nickname", "email", "hire_date"}


def read_sheet():
    """Read employee data from Google Sheet."""
    creds = authenticate_sheets()
    rows = read_spreadsheet(creds, SHEET_ID)

    if not rows or len(rows) < 2:
        logger.warning("Sheet is empty or has no data rows")
        return []

    headers = [h.strip().lower() for h in rows[0]]
    employees = []
    for row in rows[1:]:
        if len(row) < len(headers):
            row.extend([""] * (len(headers) - len(row)))
        emp = {}
        for i, h in enumerate(headers):
            val = row[i].strip() if i < len(row) and row[i] else ""
            emp[h] = val
        if emp.get("emp_id"):
            # Parse boolean
            active = emp.get("is_active", "").lower()
            emp["is_active"] = active in ("true", "yes", "1")
            # Parse dates — Google Sheets exports as M/D/YYYY
            for dt_field in ("hire_date", "resignation_date", "effective_date"):
                val = emp.get(dt_field, "").strip()
                if val and val not in ("None", "FALSE", "TRUE"):
                    parsed = None
                    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y",
                                "%B %d, %Y", "%b %d, %Y"):
                        try:
                            parsed = datetime.strptime(val, fmt).date()
                            break
                        except ValueError:
                            continue
                    emp[dt_field] = parsed
                else:
                    emp[dt_field] = None
            employees.append(emp)

    logger.info(f"Read {len(employees)} employees from Google Sheet")
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

    # Check for employees in DB but not in sheet (potential resignations)
    sheet_ids = {e["emp_id"] for e in sheet_employees}
    for emp_id, existing in current.items():
        if emp_id not in sheet_ids and existing.get("is_active"):
            resigned_employees.append(existing)

    # Report
    print(f"\n=== Sync Summary ===")
    print(f"  New employees: {len(new_employees)}")
    print(f"  Updated employees: {len(updated_employees)}")
    print(f"  Potential resignations: {len(resigned_employees)}")

    if new_employees:
        print(f"\n--- New Employees ---")
        for e in new_employees:
            print(f"  {e['emp_id']} | {e.get('full_name', '')} | {e.get('position', '')} | {e.get('email', '')}")

    if updated_employees:
        print(f"\n--- Updates ---")
        for u in updated_employees:
            emp = u["emp"]
            print(f"  {emp['emp_id']} | {emp.get('full_name', '')}")
            for field, (old, new) in u["tracked_changes"].items():
                print(f"    {field}: {old} -> {new}  [NEW HISTORY ROW]")
            for field, (old, new) in u["simple_changes"].items():
                print(f"    {field}: {old} -> {new}  [in-place update]")

    if resigned_employees:
        print(f"\n--- Potential Resignations (in DB but not in sheet) ---")
        for e in resigned_employees:
            print(f"  {e['emp_id']} | {e.get('full_name', '')} | {e.get('email', '')}")

    if not apply:
        print(f"\n=== DRY RUN (use --apply to execute) ===")
        return

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
                " resignation_date, effective_date, change_reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22) "
                "ON CONFLICT (emp_id, effective_date) DO NOTHING",
                e.get("emp_id"), e.get("last_name"), e.get("first_name"), e.get("middle_name"),
                e.get("full_name"), e.get("nickname"), e.get("email"),
                e.get("position"), e.get("role2"), e.get("carrier"), e.get("carrier_group"),
                e.get("cluster"), e.get("division"), e.get("sub_division"),
                e.get("work_schedule"), e.get("shift_schedule"), e.get("employment_status"),
                e.get("hire_date"), e.get("is_active", True), e.get("resignation_date"),
                e.get("hire_date") or effective_date, "New employee from Google Sheet sync",
            ),
            description=f"insert {emp.get('full_name', '')}",
        )
        applied += 1

    # Updates
    for u in updated_employees:
        emp = u["emp"]

        # Tracked changes → new history row
        if u["tracked_changes"]:
            changes = ", ".join(f"{k}: {old}->{new}" for k, (old, new) in u["tracked_changes"].items())
            retry_db(
                lambda e=emp, c=changes: db.execute(
                    "INSERT INTO reference.ref_employees "
                    "(emp_id, last_name, first_name, middle_name, full_name, nickname, email, "
                    " position, role2, carrier, carrier_group, cluster, division, sub_division, "
                    " work_schedule, shift_schedule, employment_status, hire_date, is_active, "
                    " resignation_date, effective_date, change_reason) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22) "
                    "ON CONFLICT (emp_id, effective_date) DO UPDATE SET "
                    "position=EXCLUDED.position, role2=EXCLUDED.role2, carrier=EXCLUDED.carrier, "
                    "carrier_group=EXCLUDED.carrier_group, cluster=EXCLUDED.cluster, "
                    "division=EXCLUDED.division, sub_division=EXCLUDED.sub_division, "
                    "work_schedule=EXCLUDED.work_schedule, shift_schedule=EXCLUDED.shift_schedule, "
                    "employment_status=EXCLUDED.employment_status, is_active=EXCLUDED.is_active, "
                    "resignation_date=EXCLUDED.resignation_date, change_reason=EXCLUDED.change_reason, "
                    "updated_at=NOW()",
                    e.get("emp_id"), e.get("last_name"), e.get("first_name"), e.get("middle_name"),
                    e.get("full_name"), e.get("nickname"), e.get("email"),
                    e.get("position"), e.get("role2"), e.get("carrier"), e.get("carrier_group"),
                    e.get("cluster"), e.get("division"), e.get("sub_division"),
                    e.get("work_schedule"), e.get("shift_schedule"), e.get("employment_status"),
                    e.get("hire_date"), e.get("is_active", True), e.get("resignation_date"),
                    effective_date, f"Sheet sync: {c}",
                ),
                description=f"update tracked {emp.get('full_name', '')}",
            )
            applied += 1

        # Simple changes → update latest row in place
        if u["simple_changes"] and not u["tracked_changes"]:
            existing = u["existing"]
            for field, (old, new) in u["simple_changes"].items():
                retry_db(
                    lambda eid=emp["emp_id"], ed=existing["effective_date"], f=field, v=emp.get(field): db.execute(
                        f"UPDATE reference.ref_employees SET {f} = $1, updated_at = NOW() "
                        f"WHERE emp_id = $2 AND effective_date = $3",
                        v, eid, ed,
                    ),
                    description=f"update simple {emp.get('full_name', '')} {field}",
                )
            applied += 1

    print(f"\nApplied {applied} changes")


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
