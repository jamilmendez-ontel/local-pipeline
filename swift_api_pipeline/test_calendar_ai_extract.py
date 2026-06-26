"""Unit tests for AI extraction JSON handling (no network). Run:
    cd swift_api_pipeline && python -m pytest test_calendar_ai_extract.py -v
"""
from calendar_ai_extract import _parse_ai_json


def test_parse_ai_json_with_fences_and_prose():
    text = 'Sure!\n```json\n{"event_kind":"leave","leave_type":"VL","team":"CRTV",' \
           '"person":"Nicolai","person_note":null,"rest_day_of_week":null}\n```'
    out = _parse_ai_json(text)
    assert out["leave_type"] == "VL"
    assert out["team"] == "CRTV"
    assert out["person"] == "Nicolai"


def test_parse_ai_json_garbage_returns_none():
    assert _parse_ai_json("no json here") is None
