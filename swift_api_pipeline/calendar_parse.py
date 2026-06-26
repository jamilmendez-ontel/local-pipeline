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
