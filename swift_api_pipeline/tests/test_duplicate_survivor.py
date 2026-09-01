"""Tests for the duplicate-group survivor rule in _resolve_duplicate_for_action.

When a member removes one entry of a pending duplicate group with 2+ others
remaining, the resolver must not pick the survivor by latest end_time: for
same-start drifted snapshots the latest end is the longest runaway copy
(czarina 2026-08-17: 2.07h real session auto-removed, 11.07h runaway kept;
8 groups zeroed since 2026-07-30, ~10.2h real work lost). The survivor is the
shortest remaining non-zero snapshot, and entries already covered by an active
removal are never eligible.

Run: python -m pytest tests/test_duplicate_survivor.py
"""
from datetime import datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import _resolve_duplicate_for_action


class FakeDB:
    """Sync stand-in for PipelineDB: canned review + removal rows, records writes."""

    def __init__(self, review, removals=()):
        self.review = review
        self.removals = list(removals)
        self.executed = []      # (sql, args) from execute()
        self.fetched_sql = []   # sql seen by fetch()

    def fetchrow(self, sql, *args):
        return self.review

    def fetch(self, sql, *args):
        self.fetched_sql.append(sql)
        return self.removals

    def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 8, 17, 21, 0, tzinfo=timezone.utc)

# Czarina-shaped 3-way group: A = real short session, B + C = runaway snapshots.
# Labels are assigned duration-ascending at group creation.
END_A = datetime(2026, 8, 17, 23, 4, tzinfo=timezone.utc)
END_B = datetime(2026, 8, 18, 5, 59, tzinfo=timezone.utc)
END_C = datetime(2026, 8, 18, 8, 4, tzinfo=timezone.utc)
DUR_A, DUR_B, DUR_C = 124.0, 539.0, 664.0


def _entries_abc():
    return [
        {"label": "A", "start_time": START.isoformat(), "end_time": END_A.isoformat(), "duration_min": DUR_A},
        {"label": "B", "start_time": START.isoformat(), "end_time": END_B.isoformat(), "duration_min": DUR_B},
        {"label": "C", "start_time": START.isoformat(), "end_time": END_C.isoformat(), "duration_min": DUR_C},
    ]


def _review(entries):
    return {
        "group_id": "g-test-1",
        "project_did": "p1",
        "project": "TECH-OPS: TS19",
        "user_email": "member@x.co",
        "start_time": START,
        "site_name": "SITE",
        "site_id": None,
        "task": "2. Live Review Complete",
        "status": "notified",
        "entries": entries,
    }


def _acted(end, dur):
    return {
        "project_did": "p1", "project": "TECH-OPS: TS19",
        "user_email": "member@x.co", "start_time": START,
        "site_name": "SITE", "site_id": None, "task": "2. Live Review Complete",
        "end_time": end, "duration_min": dur,
    }


def _update_args(db):
    calls = [(s, a) for s, a in db.executed if "duplicate_reviews" in s and "UPDATE" in s]
    assert len(calls) == 1, f"expected exactly one duplicate_reviews UPDATE, got {len(calls)}"
    return calls[0][1]  # (selected, rejected, now, group_id)


def _sibling_removal_durs(db):
    return [a[9] for s, a in db.executed if "entry_removals" in s and "INSERT" in s]


def test_remove_on_three_way_group_keeps_shortest_not_latest():
    db = FakeDB(_review(_entries_abc()))
    _resolve_duplicate_for_action(db, _acted(END_B, DUR_B), "remove", NOW)

    selected, rejected = _update_args(db)[0], _update_args(db)[1]
    assert selected == "A", f"survivor must be the shortest real snapshot, got {selected!r}"
    # The real session must never get an auto removal; only runaway C needs one.
    assert _sibling_removal_durs(db) == [DUR_C]
    assert {r["duration_min"] for r in rejected} == {DUR_B, DUR_C}


def test_remove_never_selects_an_already_removed_sibling():
    # A already has an active removal and the LATEST end_time; removing C must
    # leave B as the survivor, not resurrect A.
    entries = _entries_abc()
    entries[0]["end_time"] = datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc).isoformat()
    db = FakeDB(_review(entries),
                removals=[{"end_time": datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
                           "duration_min": DUR_A}])
    _resolve_duplicate_for_action(db, _acted(END_C, DUR_C), "remove", NOW)

    assert _update_args(db)[0] == "B"
    # Eligibility must ignore reverted removals server-side.
    assert any("REVERTED" in s for s in db.fetched_sql)


def test_remove_of_last_alive_entry_resolves_with_no_survivor():
    entries = _entries_abc()[:2]  # two-way group
    db = FakeDB(_review(entries),
                removals=[{"end_time": END_A, "duration_min": DUR_A}])
    _resolve_duplicate_for_action(db, _acted(END_B, DUR_B), "remove", NOW)

    selected, rejected = _update_args(db)[0], _update_args(db)[1]
    assert selected is None, f"a fully-removed group has no survivor, got {selected!r}"
    assert {r["duration_min"] for r in rejected} == {DUR_A, DUR_B}


def test_two_way_remove_keeps_the_other_entry():
    entries = _entries_abc()[:2]
    db = FakeDB(_review(entries))
    _resolve_duplicate_for_action(db, _acted(END_A, DUR_A), "remove", NOW)

    assert _update_args(db)[0] == "B"
    assert _sibling_removal_durs(db) == []


def test_correct_action_still_keeps_the_corrected_entry():
    db = FakeDB(_review(_entries_abc()))
    _resolve_duplicate_for_action(db, _acted(END_B, DUR_B), "correct", NOW)

    selected = _update_args(db)[0]
    assert selected == "B"
    # Both non-selected entries need real removal rows (Milton Frank invariant).
    assert sorted(_sibling_removal_durs(db)) == [DUR_A, DUR_C]
