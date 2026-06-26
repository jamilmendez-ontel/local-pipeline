# calendar_lookups.py
"""Load the reference maps + employee index once per run."""
from calendar_normalize import build_employee_index


def load_lookups(db) -> dict:
    code_rows = db.fetch("SELECT code, label, category FROM reference.ref_leave_code")
    code_map = {r["code"].upper(): (r["label"], r["category"]) for r in code_rows}

    team_rows = db.fetch("SELECT team_raw, team_canonical, level FROM reference.ref_calendar_team")
    team_map = {r["team_raw"].strip().lower(): (r["team_canonical"], r["level"]) for r in team_rows}

    # ref_employees is SCD-shaped (effective_date history). Take the latest
    # effective row per emp_id so team_normalized uses the current carrier_group
    # and each employee appears once in the match index (no false ambiguity).
    emp_rows = db.fetch(
        "SELECT DISTINCT ON (emp_id) emp_id, full_name, first_name, nickname, "
        "       carrier_group, cluster "
        "FROM reference.ref_employees "
        "ORDER BY emp_id, effective_date DESC NULLS LAST")
    emps = [dict(r) for r in emp_rows]
    emp_index = build_employee_index(emps)
    emp_by_id = {e["emp_id"]: e for e in emps if e.get("emp_id")}

    return {"code_map": code_map, "team_map": team_map,
            "emp_index": emp_index, "emp_by_id": emp_by_id}
