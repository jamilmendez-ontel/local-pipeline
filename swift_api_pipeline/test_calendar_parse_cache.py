"""Unit tests for the parse-cache resolver using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_parse_cache.py -v
"""
from calendar_parse_cache import summary_key, resolve


class FakeDB:
    """Minimal db stub: in-memory agent.calendar_summary_parse."""
    def __init__(self, rows=None):
        self.rows = rows or {}     # summary_key -> record dict
        self.writes = 0

    def fetchrow(self, query, *args):
        return self.rows.get(args[0])

    def execute(self, query, *args):
        self.writes += 1
        self.rows[args[0]] = {"summary_key": args[0]}
        return "INSERT 0 1"


def test_summary_key_collapses_whitespace():
    assert summary_key("  VL  -   Zeta -  Luis ") == "VL - Zeta - Luis"


def test_resolve_clean_uses_deterministic_and_writes_cache():
    db = FakeDB()
    calls = {"n": 0}

    def ai_fn(summary, client=None):
        calls["n"] += 1
        return {"event_kind": "other"}

    r = resolve(db, "VL - Zeta - Luis", ai_fn=ai_fn)
    assert r["parse_source"] == "deterministic"
    assert r["leave_type"] == "VL"
    assert calls["n"] == 0          # AI not called for a clean parse
    assert db.writes == 1           # result cached


def test_resolve_low_confidence_calls_ai():
    db = FakeDB()
    calls = {"n": 0}

    def ai_fn(summary, client=None):
        calls["n"] += 1
        return {"event_kind": "leave", "leave_type": "VL", "team": "CRTV",
                "person": "Nicolai", "person_note": None, "rest_day_of_week": None,
                "confidence": 0.75, "parse_source": "ai", "needs_review": False}

    r = resolve(db, "VL_CRTV_Nicolai", ai_fn=ai_fn)
    assert calls["n"] == 1
    assert r["parse_source"] == "ai"
    assert r["team"] == "CRTV"


def test_resolve_cache_hit_skips_both():
    cached = {"summary_key": "VL - Zeta - Luis", "event_kind": "leave",
              "leave_type": "VL", "team": "Zeta", "person": "Luis",
              "person_note": None, "rest_day_of_week": None, "confidence": 0.95,
              "parse_source": "deterministic", "needs_review": False}
    db = FakeDB(rows={"VL - Zeta - Luis": cached})

    def ai_fn(summary, client=None):
        raise AssertionError("AI must not be called on cache hit")

    r = resolve(db, "VL - Zeta - Luis", ai_fn=ai_fn)
    assert r["leave_type"] == "VL"
    assert db.writes == 0
