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
