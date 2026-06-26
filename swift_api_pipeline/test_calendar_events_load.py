"""Tests for staging load + tombstone using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_events_load.py -v
"""
from calendar_events_load import load_staging, _UPSERT_COLS


class FakeDB:
    def __init__(self):
        self.upserts = []
        self.tombstones = []

    def fetch(self, query, *args):
        # Return empty lists for all reference table lookups (code_map, team_map, emp_rows).
        return []

    def fetchrow(self, query, *args):
        # No cached person-match entries.
        return None

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
    # cancelled ids are tombstoned in one bulk UPDATE: args[0] is the id list.
    assert len(db.tombstones) == 1
    assert "e2" in db.tombstones[0][0]
    # Enrichment ran to completion: the new normalized columns flow through
    # build_row into the upserted tuple without KeyError, i.e. not silently
    # swallowed into `skipped`. With an empty FakeDB.fetch, normalize_leave_type
    # falls back to the raw code and the person stays unmatched.
    assert len(db.upserts) == 1
    assert db.upserts[0][_UPSERT_COLS.index("leave_type_normalized")] == "VL"
    assert db.upserts[0][_UPSERT_COLS.index("person_normalized")] is None
    assert db.upserts[0][_UPSERT_COLS.index("person_match_source")] == "unmatched"


def test_many_cancelled_tombstoned_in_one_round_trip():
    db = FakeDB()
    events = [{"id": f"c{i}", "status": "cancelled", "start": {}, "end": {}}
              for i in range(2000)]
    counts = load_staging(db, "run-1", events, resolve_fn=lambda d, s: _shape())
    assert counts["tombstoned"] == 2000
    assert counts["upserted"] == 0
    # 2000 cancellations must be ONE execute, not 2000 (the backfill perf fix).
    assert len(db.tombstones) == 1
    assert len(db.tombstones[0][0]) == 2000
