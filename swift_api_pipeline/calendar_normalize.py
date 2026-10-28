"""Pure normalization functions for calendar events. No DB, no network: all
lookups are passed in, so these are unit-testable in isolation."""
import re


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


def build_employee_index(emp_rows: list[dict]) -> dict[str, list[dict]]:
    """Map lower(name) -> list of employee dicts, indexing nickname, first_name,
    and full_name. Include resigned employees (historical leave references them)."""
    idx = {}
    for e in emp_rows:
        for name in (e.get("nickname"), e.get("first_name"), e.get("full_name")):
            if name and name.strip():
                idx.setdefault(name.strip().lower(), []).append(e)
    return idx


def match_person_deterministic(person_raw: str | None, team_raw: str | None, emp_index: dict[str, list[dict]], team_map: dict) -> tuple[dict | None, str]:
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


# --- person_note (Phase 2 Wave 2) -------------------------------------------
# Deterministic regex, no AI. The raw `person_note` is kept; this fills
# `person_note_normalized`. Canonical forms:
#   start time  -> "2:00 PM onwards"   (also "After 4 PM", "In by 12PM", "Working 12 PM")
#   end time    -> "Until 12:00 PM"    (also "Out 2PM", "only until 1pm")
#   window      -> "9:00 AM - 2:00 PM" (also "9am to 2pm", "2-6PM", "9-1 PM")
#   bare time   -> "2:00 PM"
#   time zone   -> kept after the time: "10:00 AM - 3:00 PM EST", "11:00 AM PHT onwards"
# Notes that are an employee-id suffix ("VL - CG1 - Ana - 200601" parses the id
# into person_note) are identifiers, not notes, and normalize to NULL. Anything
# else passes through with whitespace and casing standardized only.
_T = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|nn|noon)?"
_TZ_RE = re.compile(r"\s*\b(est|edt|et|pht)\b\s*", re.I)
_EMP_ID_RE = re.compile(r"-?\d{6,7}")
_NOISE_RE = re.compile(r"\(\d+\)")
_RANGE_RE = re.compile(rf"(?:working\s+)?{_T}\s*(?:-|to)\s*{_T}", re.I)
# A start time needs a lead-in word or a trailing "onwards" ("2 pm" alone is a bare time).
_START_RE = re.compile(
    rf"(?:(?:t?w[oe]rking)|work|will\s+work\s+at|reports\s+at|in\s+by|in|"
    rf"available\s+by|available|after)\s+{_T}(?:\s+onwards)?"
    rf"|(?:(?:t?w[oe]rking)\s+)?{_T}\s+onwards", re.I)
_UNTIL_RE = re.compile(
    rf"(?:working\s+|only\s+)?(?:until|out)\s+{_T}(?:\s+only)?", re.I)
_BARE_RE = re.compile(_T, re.I)
_HALF_DAY_RE = re.compile(r"half\s*day", re.I)


def _bare_meridiem(h: int) -> str:
    """Workday rule for an hour written without AM/PM (Ontel shift is ~9-6):
    7-11 are morning, 12 and 1-6 are afternoon."""
    return "AM" if 7 <= h <= 11 else "PM"


def _clock(h: str, m: str | None, mer: str | None, default_mer: str | None = None) -> str | None:
    """Format one time token as 'H:MM AM'. Returns None if it is not a clock time."""
    hour, minute = int(h), int(m) if m else 0
    if minute > 59:
        return None
    mer = (mer or "").lower()
    if mer in ("nn", "noon"):
        if hour != 12:
            return None
        mer = "pm"
    if not mer:
        if 13 <= hour <= 23:              # 24-hour clock
            hour, mer = hour - 12, "pm"
        elif 1 <= hour <= 12:
            mer = (default_mer or _bare_meridiem(hour)).lower()
        else:
            return None
    if not 1 <= hour <= 12:
        return None
    return f"{hour}:{minute:02d} {mer.upper()}"


def _range_start_meridiem(h1: int, h2: int, mer2: str) -> str:
    """A range start with no AM/PM takes the end's meridiem when it is earlier
    on the same half of the clock ('2-6PM', '7-9AM'), else the other one ('9-1 PM')."""
    mer2 = "pm" if mer2.lower() in ("nn", "noon") else mer2.lower()
    a, b = h1 % 12, h2 % 12
    if a <= b:
        return mer2
    return "am" if mer2 == "pm" else "pm"


def _sentence_case(text: str) -> str:
    """Lowercase every word except all-caps acronyms (AM, LAC, PHT), then
    capitalize the first letter."""
    words = []
    for w in text.split(" "):
        if len(w) >= 2 and w.isalpha() and w.isupper():
            words.append(w)
        else:
            words.append(w.lower())
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def id_suffix_match(note: str | None, emp_by_id: dict) -> dict | None:
    """The employee id typed at the end of an event ("VL - CG1 - Ana - 200601", parsed
    into person_note) is the reference for who is on leave: the creator is often
    someone else filing for them, and the name part is a nickname. When that id is in
    the roster it decides the person, ahead of the name cache, exact match and AI.
    An id not in the roster (a typo such as 2202325) returns None so the name match
    still runs. (Agreed 2026-10-28 by Jamil Mendez, INIT-397.)"""
    if note is None:
        return None
    s = "".join(note.split())
    if not _EMP_ID_RE.fullmatch(s):
        return None
    emp = emp_by_id.get(s.lstrip("-"))
    if emp is None:
        return None
    return {"emp_id": emp["emp_id"], "person_normalized": emp.get("full_name"),
            "confidence": 1.0, "match_source": "id_suffix"}


def normalize_person_note(note: str | None) -> str | None:
    """Clean a calendar person_note. See the module comment above for forms."""
    if note is None:
        return None
    s = " ".join(note.split())
    if not s or _EMP_ID_RE.fullmatch(s) or _NOISE_RE.fullmatch(s):
        return None

    tz_match = _TZ_RE.search(s)
    tz = tz_match.group(1).upper() if tz_match else None
    core = _TZ_RE.sub(" ", s).strip() if tz else s
    tz_sfx = f" {tz}" if tz else ""

    m = _RANGE_RE.fullmatch(core)
    if m:
        h1, m1, mer1, h2, m2, mer2 = m.groups()
        if mer2 and not mer1:
            mer1 = _range_start_meridiem(int(h1), int(h2), mer2)
        start = _clock(h1, m1, mer1)
        end = _clock(h2, m2, mer2)
        if start and end:
            return f"{start} - {end}{tz_sfx}"

    m = _UNTIL_RE.fullmatch(core)
    if m:
        t = _clock(*m.groups())
        if t:
            return f"Until {t}{tz_sfx}"

    m = _START_RE.fullmatch(core)
    if m:
        g = m.groups()
        t = _clock(*(g[:3] if g[0] else g[3:]))
        if t:
            return f"{t}{tz_sfx} onwards"

    m = _BARE_RE.fullmatch(core)
    if m and m.group(3):                  # a bare time needs AM/PM to be a time
        t = _clock(*m.groups())
        if t:
            return f"{t}{tz_sfx}"

    if _HALF_DAY_RE.fullmatch(s):
        return "Half day"
    return _sentence_case(s)
