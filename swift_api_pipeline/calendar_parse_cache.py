"""Resolve a calendar summary to the conformed parse shape, caching the result
in agent.calendar_summary_parse so incremental and full-refresh runs produce
identical output and repeat strings cost nothing."""
import re

from calendar_parse import deterministic_parse, CONFIDENCE_GATE
from calendar_ai_extract import extract_with_ai, PROMPT_VERSION, MODEL

_CACHE_COLS = ("event_kind", "leave_type", "team", "person", "person_note",
               "rest_day_of_week", "confidence", "parse_source", "needs_review")


def summary_key(summary: str) -> str:
    """Whitespace-collapsed key. Do not over-normalize (would merge distinct strings)."""
    return re.sub(r"\s+", " ", (summary or "").strip())


def _row_to_shape(row) -> dict:
    return {k: row[k] for k in _CACHE_COLS}


def _write_cache(db, key: str, shape: dict):
    db.execute(
        "INSERT INTO agent.calendar_summary_parse "
        "(summary_key, event_kind, leave_type, team, person, person_note, "
        " rest_day_of_week, confidence, parse_source, needs_review, model, prompt_version) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
        "ON CONFLICT (summary_key) DO UPDATE SET "
        "  event_kind=EXCLUDED.event_kind, leave_type=EXCLUDED.leave_type, "
        "  team=EXCLUDED.team, person=EXCLUDED.person, person_note=EXCLUDED.person_note, "
        "  rest_day_of_week=EXCLUDED.rest_day_of_week, confidence=EXCLUDED.confidence, "
        "  parse_source=EXCLUDED.parse_source, needs_review=EXCLUDED.needs_review, "
        "  model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version, extracted_at=now()",
        key, shape["event_kind"], shape["leave_type"], shape["team"], shape["person"],
        shape["person_note"], shape["rest_day_of_week"], shape["confidence"],
        shape["parse_source"], shape["needs_review"],
        MODEL if shape["parse_source"] == "ai" else None,
        PROMPT_VERSION if shape["parse_source"] == "ai" else None,
    )


def resolve(db, summary: str, ai_fn=extract_with_ai) -> dict:
    """Cache -> deterministic -> AI (if below gate). Always returns the shape."""
    key = summary_key(summary)
    row = db.fetchrow("SELECT * FROM agent.calendar_summary_parse WHERE summary_key = $1", key)
    if row is not None:
        return _row_to_shape(row)

    shape = deterministic_parse(summary)
    if shape["confidence"] < CONFIDENCE_GATE:
        shape = ai_fn(summary)
    _write_cache(db, key, shape)
    return shape
