"""Claude Haiku structured extraction for calendar summaries the deterministic
parser could not confidently handle. Returns the conformed shape; any failure
degrades to needs_review rather than raising."""
import os
import re
import json

import anthropic

from calendar_parse import KNOWN_LEAVE_CODES, KNOWN_TEAMS

MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = "2026-06-25.1"


def _build_prompt(summary: str) -> str:
    codes = ", ".join(sorted(KNOWN_LEAVE_CODES))
    teams = ", ".join(sorted(KNOWN_TEAMS))
    return (
        "You extract structured fields from one entry on a company's shared "
        "leave/work calendar. The loose convention is 'LeaveType - Team - Person "
        "(note)' but entries are inconsistent.\n\n"
        f"Known leave codes: {codes}\n"
        f"Known teams: {teams}\n\n"
        "Rules:\n"
        "- event_kind is one of: leave, holiday, birthday, training, other.\n"
        "- leave_type is a known code (or compound like 'UT/SL'); null if not leave.\n"
        "- If a weekday (Mon..Sun) sits where a person should be, set rest_day_of_week "
        "(Mon/Tue/Wed/Thu/Fri/Sat/Sun) and person null.\n"
        "- Strip trailing notes from person into person_note.\n"
        "- Birthdays, performance evaluations, trainings are NOT leave.\n\n"
        f"Entry: {json.dumps(summary)}\n\n"
        "Return ONLY a JSON object with keys: event_kind, leave_type, team, person, "
        "person_note, rest_day_of_week."
    )


def _parse_ai_json(text: str) -> dict | None:
    """Strip fences/prose and load the first {...} object. None on failure."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, json.JSONDecodeError):
        return None


def _shape_from_ai(d: dict) -> dict:
    return {
        "event_kind": d.get("event_kind") or "other",
        "leave_type": d.get("leave_type"),
        "team": d.get("team"),
        "person": d.get("person"),
        "person_note": d.get("person_note"),
        "rest_day_of_week": d.get("rest_day_of_week"),
        "confidence": 0.75,
        "parse_source": "ai",
        "needs_review": False,
    }


def _needs_review_shape() -> dict:
    return {
        "event_kind": "other", "leave_type": None, "team": None, "person": None,
        "person_note": None, "rest_day_of_week": None, "confidence": 0.0,
        "parse_source": "ai", "needs_review": True,
    }


def extract_with_ai(summary: str, client=None) -> dict:
    """Call Haiku for one summary. Degrades to needs_review on any error."""
    try:
        client = client or anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=512,
            messages=[{"role": "user", "content": _build_prompt(summary)}],
        )
        parsed = _parse_ai_json(resp.content[0].text)
        return _shape_from_ai(parsed) if parsed else _needs_review_shape()
    except Exception:
        return _needs_review_shape()
