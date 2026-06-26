"""Test reconciliation soft-deletes absent events. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_events_reconcile.py -v
"""
from calendar_events_reconcile import reconcile


class FakeDB:
    def __init__(self, staged):
        self.staged = staged           # list of event_id currently active
        self.soft_deleted = []

    def fetch(self, query, *args):
        return [{"event_id": e} for e in self.staged]

    def execute(self, query, *args):
        self.soft_deleted.append(args[0])   # array of ids
        return "OK"


def test_reconcile_soft_deletes_absent():
    db = FakeDB(staged=["e1", "e2", "e3"])
    n = reconcile(db, live_event_ids={"e1", "e3"})
    assert n == 1
    assert "e2" in db.soft_deleted[0]
