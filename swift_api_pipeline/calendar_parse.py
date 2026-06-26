"""Deterministic, pure-function parser for calendar event summaries.

No DB, no network. The summary convention is "LeaveType - Team - Person (note)"
but the source is a free-text shared calendar, so anything that does not parse
to a validated 3-part shape is returned with low confidence for AI fallback.
"""
import re

KNOWN_LEAVE_CODES = {
    "RD", "RDOT", "RDO", "VL", "SL", "EL", "SDL", "UT", "BL", "ML",
    "PL", "SPL", "STL", "BRL", "LR", "WW", "LAC", "HD", "PH", "LWOP",
}

KNOWN_TEAMS = {
    "CG1", "CG2", "CG3", "ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA",
    "QPI", "CRTV", "CRTVS", "ADMIN AND OPS", "ADMIN & OPS", "ACCTG", "ACCOUNTING",
    "R&D", "DA", "T&A", "TNA", "HR", "SWIFT", "MKTG", "MARKETING",
    "TS ADMIN", "TS OPS", "PHI DS", "PHIDS", "PHIDSM", "DSM", "PHI HR",
    "TRAINEE", "SD", "AI", "AIE", "T&D",
}


def normalize_separators(summary: str) -> str:
    """Fix inconsistent dash spacing so split(' - ') works, including teams
    that end in a digit (e.g. 'CG1- Angelica')."""
    s = re.sub(r" -(\S)", r" - \1", summary)        # " -X" -> " - X"
    s = re.sub(r"(\w)- (?=\S)", r"\1 - ", s)        # "X- "  -> "X - " (\w includes digits)
    return s


_WEEKDAYS = {
    "MON": "Mon", "MONDAY": "Mon",
    "TUE": "Tue", "TUES": "Tue", "TUESDAY": "Tue",
    "WED": "Wed", "WEDS": "Wed", "WEDNESDAY": "Wed",
    "THU": "Thu", "THUR": "Thu", "THURS": "Thu", "THURSDAY": "Thu",
    "FRI": "Fri", "FRIDAY": "Fri",
    "SAT": "Sat", "SATURDAY": "Sat",
    "SUN": "Sun", "SUNDAY": "Sun",
}
_NOTE_PAREN_RE = re.compile(r"\s*\(([^)]+)\)\s*$")


def canonical_weekday(token: str) -> str | None:
    """Map a day-of-week token to canonical 'Mon'..'Sun', else None."""
    if not token:
        return None
    return _WEEKDAYS.get(token.strip().upper())


def split_note(person_raw: str) -> tuple[str | None, str | None]:
    """Separate a trailing note from a person name. Handles a parenthetical
    '(note)' and an unparenthesized ' - note' tail."""
    if not person_raw:
        return None, None
    m = _NOTE_PAREN_RE.search(person_raw)
    if m:
        return _NOTE_PAREN_RE.sub("", person_raw).strip(), m.group(1).strip()
    if " - " in person_raw:
        head, tail = person_raw.split(" - ", 1)
        return head.strip(), tail.strip()
    return person_raw.strip(), None


def classify_kind(summary: str, leave_type: str | None) -> str:
    """Coarse event-kind classification. Order matters: holiday/birthday/training
    are recognized by summary text; a known leave code wins otherwise."""
    s = (summary or "").lower()
    if not summary or not summary.strip():
        return "other"
    if "holiday" in s or summary.startswith(("PH:", "PH Holiday:", "US:")):
        return "holiday"
    if "birthday" in s or "bday" in s or "b-day" in s or "anniversary" in s:
        return "birthday"
    if any(k in s for k in ("refresher", "training", "walkthrough", "course", "webinar")):
        return "training"
    code = (leave_type or "").upper().replace("/", "")
    if code in KNOWN_LEAVE_CODES or (leave_type and "/" in leave_type):
        return "leave"
    return "other"


CONFIDENCE_GATE = 0.8

_BASE_SHAPE_KEYS = (
    "event_kind", "leave_type", "team", "person", "person_note",
    "rest_day_of_week", "confidence", "parse_source", "needs_review",
)


def _shape(**kw) -> dict:
    """Construct a parse result shape with defaults."""
    base = {k: None for k in _BASE_SHAPE_KEYS}
    base.update({"parse_source": "deterministic", "needs_review": False,
                 "confidence": 0.0, "event_kind": "other"})
    base.update(kw)
    return base


def deterministic_parse(summary: str) -> dict:
    """Parse a summary into the conformed shape with a calibrated confidence.
    Confidence is high only when the result is a positively-validated leave
    row; anything ambiguous returns < CONFIDENCE_GATE for AI fallback."""
    summary = (summary or "").strip()
    kind = classify_kind(summary, None)

    # Non-leave kinds: leave-specific fields stay null; confidence high when the
    # text signal is unambiguous (holiday/birthday/training keywords).
    if kind in ("holiday", "birthday", "training"):
        return _shape(event_kind=kind, confidence=0.9)
    if not summary:
        return _shape(event_kind="other", confidence=0.9)

    norm = normalize_separators(summary)
    parts = [p.strip() for p in norm.split(" - ")] if " - " in norm else None
    if not parts or len(parts) < 3:
        # 1-2 parts, underscores, or unsplittable: do not trust it.
        return _shape(event_kind="other", confidence=0.3)

    leave_type = parts[0]
    team = parts[1]
    person_raw = " - ".join(parts[2:])

    code_ok = leave_type.upper().replace("/", "") in KNOWN_LEAVE_CODES or "/" in leave_type
    team_ok = team.upper() in KNOWN_TEAMS

    weekday = canonical_weekday(person_raw)
    if weekday:
        person, note = None, None
    else:
        person, note = split_note(person_raw)

    # Confidence: both code and team validated -> trust; one unknown -> route to AI.
    if code_ok and team_ok:
        confidence = 0.95
    elif code_ok or team_ok:
        confidence = 0.6
    else:
        confidence = 0.3

    return _shape(
        event_kind="leave" if code_ok else "other",
        leave_type=leave_type if code_ok else None,
        team=team,
        person=person,
        person_note=note,
        rest_day_of_week=weekday if code_ok else None,
        confidence=confidence,
    )
