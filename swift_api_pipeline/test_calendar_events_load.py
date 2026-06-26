"""Tests for staging load + tombstone using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_events_load.py -v
"""
from calendar_events_load import load_staging


class FakeDB:
    def __init__(self):
        self.upserts = []
        self.tombstones = []

    def execute(self, query, *args):
        if "is_deleted = true" in query:
            self.tombstones.append(args)
        return "OK"

    def executemany(self, query, args):
        self.upserts.extend(args)


def _shape():
    return {"event_kind": "leave", "leave_type": "VL", "team": "Zeta",
            "person": "Luis", "person_note": None, "rest_day_of_week": None,
            "confidence": 0.95, "parse_source": "deterministic", "needs_review": False}


def test_load_upserts_active_and_tombstones_cancelled():
    db = FakeDB()
    events = [
        {"id": "e1", "summary": "VL - Zeta - Luis", "status": "confirmed",
         "start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"},
         "created": "2026-01-01T00:00:00Z", "updated": "2026-01-02T00:00:00Z",
         "creator": {"email": "a@ontel.co"}},
        {"id": "e2", "status": "cancelled", "start": {}, "end": {}},
    ]
    counts = load_staging(db, "run-1", events, resolve_fn=lambda d, s: _shape())
    assert counts["upserted"] == 1
    assert counts["tombstoned"] == 1
    assert db.tombstones[0][0] == "e2"
