"""Unit tests for the person resolver using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_person_cache.py -v
"""
from calendar_person_cache import resolve_person
from calendar_normalize import build_employee_index

EMPLOYEES = [
    {"emp_id": "E1", "full_name": "Edward Cruz", "first_name": "Edward", "nickname": "Ed",
     "carrier_group": "CG1 - Verizon", "cluster": "Alpha"},
    {"emp_id": "E3", "full_name": "Prince Uy", "first_name": "Prince", "nickname": None,
     "carrier_group": "Creatives", "cluster": None},
]
EMP_INDEX = build_employee_index(EMPLOYEES)
TEAM_MAP = {"crtv": ("Creatives", "carrier_group"), "cg1": ("CG1 - Verizon", "carrier_group")}


class FakeDB:
    def __init__(self):
        self.rows = {}      # (person_raw, team_raw) -> record
        self.writes = 0

    def fetchrow(self, query, *args):
        return self.rows.get((args[0], args[1]))

    def execute(self, query, *args):
        self.writes += 1
        self.rows[(args[0], args[1])] = {
            "person_raw": args[0], "team_raw": args[1], "emp_id": args[2],
            "person_normalized": args[3], "confidence": args[4], "match_source": args[5],
        }
        return "INSERT 0 1"


def test_resolve_exact_no_ai_and_caches():
    db = FakeDB()
    calls = {"n": 0}
    def ai_fn(*a, **k):
        calls["n"] += 1
        return None
    r = resolve_person(db, "Prince", "CRTV", EMP_INDEX, TEAM_MAP, ai_fn=ai_fn)
    assert r["emp_id"] == "E3" and r["person_normalized"] == "Prince Uy"
    assert r["match_source"] == "exact"
    assert calls["n"] == 0 and db.writes == 1


def test_resolve_cache_hit_skips_match():
    db = FakeDB()
    db.rows[("Prince", "CRTV")] = {"person_raw": "Prince", "team_raw": "CRTV",
        "emp_id": "E3", "person_normalized": "Prince Uy", "confidence": 1.0,
        "match_source": "exact"}
    r = resolve_person(db, "Prince", "CRTV", EMP_INDEX, TEAM_MAP)
    assert r["emp_id"] == "E3" and db.writes == 0


def test_resolve_unmatched_calls_ai_then_caches():
    db = FakeDB()
    def ai_fn(person_raw, team_raw, candidate_names):
        return {"emp_id": "E1", "person_normalized": "Edward Cruz", "confidence": 0.7}
    r = resolve_person(db, "Eddie", "CG1", EMP_INDEX, TEAM_MAP, ai_fn=ai_fn)
    assert r["emp_id"] == "E1" and r["match_source"] == "ai"
    assert db.writes == 1


def test_resolve_ai_gives_up_marks_unmatched():
    db = FakeDB()
    r = resolve_person(db, "Ghost", "CG1", EMP_INDEX, TEAM_MAP, ai_fn=lambda *a, **k: None)
    assert r["emp_id"] is None and r["match_source"] == "unmatched"
    assert db.writes == 1


def test_resolve_null_person_no_write():
    db = FakeDB()
    r = resolve_person(db, None, "CG1", EMP_INDEX, TEAM_MAP)
    assert r["emp_id"] is None and r["match_source"] == "unmatched"
    assert db.writes == 0
