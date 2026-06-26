"""Pure normalization functions for calendar events. No DB, no network: all
lookups are passed in, so these are unit-testable in isolation."""


def normalize_leave_type(code: str | None, code_map: dict) -> tuple[str | None, str | None]:
    """Map a leave code to (label, category). Case-insensitive. Compound codes
    (e.g. 'UT/SL') join part labels with ' + ' and category 'compound'. Unknown
    codes fall back to the raw code with category None.

    `code_map` maps UPPERCODE -> (label, category)."""
    if not code or not code.strip():
        return None, None
    raw = code.strip()
    key = raw.upper().replace(" ", "")
    if "/" in key:
        parts = [p for p in key.split("/") if p]
        labels = [(code_map.get(p, (None, None))[0] or p) for p in parts]
        return " + ".join(labels), "compound"
    label, category = code_map.get(key, (None, None))
    return (label or raw), category


def normalize_team(emp: dict | None, team_raw: str | None, team_map: dict) -> tuple[str | None, str | None]:
    """Person-derived team with label fallback. If a matched employee is given,
    use their carrier_group. Otherwise fall back to the cleaned label map
    (RD rest-day rows, unmatched people). (None, None) when neither applies."""
    if emp and emp.get("carrier_group"):
        return emp["carrier_group"], "carrier_group"
    if team_raw and team_raw.strip():
        return team_map.get(team_raw.strip().lower(), (None, None))
    return None, None


def build_employee_index(emp_rows):
    """Map lower(name) -> list of employee dicts, indexing nickname, first_name,
    and full_name. Include resigned employees (historical leave references them)."""
    idx = {}
    for e in emp_rows:
        for name in (e.get("nickname"), e.get("first_name"), e.get("full_name")):
            if name and name.strip():
                idx.setdefault(name.strip().lower(), []).append(e)
    return idx


def match_person_deterministic(person_raw, team_raw, emp_index, team_map):
    """Match a calendar person to an employee. Returns (emp, source) where source
    is exact / ambiguous / unmatched. Multiple name matches are disambiguated by
    the row's team (candidate carrier_group or cluster equals the team canonical)."""
    if not person_raw or not person_raw.strip():
        return None, "unmatched"
    cands = emp_index.get(person_raw.strip().lower(), [])
    if not cands:
        return None, "unmatched"
    if len(cands) == 1:
        return cands[0], "exact"
    canon, _level = team_map.get((team_raw or "").strip().lower(), (None, None))
    if canon:
        for c in cands:
            if c.get("carrier_group") == canon or c.get("cluster") == canon:
                return c, "exact"
    return None, "ambiguous"
