"""Tests for the member-only duplicate rule in _resolve_duplicate_for_action.

Rule (Jamil 2026-09-17): the system never removes a timer entry on a member's
behalf. A member's Remove (or Edit) touches exactly the entry the member acted
on. Afterwards the resolver only decides whether the duplicate group is
finished:

* alive entries (no active removal) are re-clustered by time overlap;
* if any cluster of 2+ alive copies remains, the group stays open (reminders
  keep going) and its `entries` snapshot is narrowed to those copies, so the
  provisional latest-end rule in rebuild_timer_clean() hides only real
  leftovers and every other alive entry counts;
* otherwise the group resolves with nothing rejected.

Background (nath 2026-09-15): two 8h runaway snapshots bridged a real 08:05
session and a real 14:43 session into one overlap cluster. The member removed
both runaways; the old collapse-to-one-survivor rule then set aside the 08:05
session as a "duplicate" of 14:43 although the two never overlapped.

Run: python -m pytest tests/test_duplicate_member_removals.py
"""
from datetime import datetime, timezone
import json
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
        if "'resolved'" in sql:
            raise AssertionError("resolver must not look up resolved groups any more")
        return self.review

    def fetch(self, sql, *args):
        self.fetched_sql.append(sql)
        return self.removals

    def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

# Nath-shaped mixed cluster (UTC): B = real 12:05-13:32, A = real 18:43-19:13,
# C + D = 8h runaway snapshots 12:05-20:12 that overlap both real sessions.
START = datetime(2026, 9, 15, 12, 5, 18, tzinfo=timezone.utc)
START_A = datetime(2026, 9, 15, 18, 43, 40, tzinfo=timezone.utc)
END_A = datetime(2026, 9, 15, 19, 13, 32, tzinfo=timezone.utc)
END_B = datetime(2026, 9, 15, 13, 32, 6, tzinfo=timezone.utc)
END_C = datetime(2026, 9, 15, 20, 12, 41, tzinfo=timezone.utc)
END_D = datetime(2026, 9, 15, 20, 12, 42, tzinfo=timezone.utc)
DUR_A, DUR_B, DUR_C, DUR_D = 29.88, 86.8, 487.38, 487.39


def _nath_entries():
    return [
        {"label": "A", "start_time": START_A.isoformat(), "end_time": END_A.isoformat(), "duration_min": DUR_A},
        {"label": "B", "start_time": START.isoformat(), "end_time": END_B.isoformat(), "duration_min": DUR_B},
        {"label": "C", "start_time": START.isoformat(), "end_time": END_C.isoformat(), "duration_min": DUR_C},
        {"label": "D", "start_time": START.isoformat(), "end_time": END_D.isoformat(), "duration_min": DUR_D},
    ]


# Same-start drift copies (czarina shape): one timer, three snapshots.
S_END_A = datetime(2026, 8, 17, 23, 4, tzinfo=timezone.utc)
S_END_B = datetime(2026, 8, 18, 5, 59, tzinfo=timezone.utc)
S_END_C = datetime(2026, 8, 18, 8, 4, tzinfo=timezone.utc)
S_DUR_A, S_DUR_B, S_DUR_C = 124.0, 539.0, 664.0


def _same_start_entries():
    return [
        {"label": "A", "start_time": START.isoformat(), "end_time": S_END_A.isoformat(), "duration_min": S_DUR_A},
        {"label": "B", "start_time": START.isoformat(), "end_time": S_END_B.isoformat(), "duration_min": S_DUR_B},
        {"label": "C", "start_time": START.isoformat(), "end_time": S_END_C.isoformat(), "duration_min": S_DUR_C},
    ]


def _review(entries, status="notified"):
    return {
        "group_id": "g-test-1",
        "project_did": "p1",
        "project": "TECH-OPS: TS20",
        "user_email": "member@x.co",
        "start_time": START,
        "site_name": None,
        "site_id": None,
        "task": "1. General Admin and Support",
        "status": status,
        "entries": entries,
    }


def _acted(end, dur, start=START):
    return {
        "project_did": "p1", "project": "TECH-OPS: TS20",
        "user_email": "member@x.co", "start_time": start,
        "site_name": None, "site_id": None, "task": "1. General Admin and Support",
        "end_time": end, "duration_min": dur,
    }


def _review_updates(db):
    return [(s, a) for s, a in db.executed if "duplicate_reviews" in s and "UPDATE" in s]


def _removal_inserts(db):
    return [(s, a) for s, a in db.executed if "entry_removals" in s and "INSERT" in s]


def _removal_updates(db):
    return [(s, a) for s, a in db.executed if "entry_removals" in s and "UPDATE" in s]


def _labels(entries_json):
    return [e["label"] for e in entries_json]


# ---------------------------------------------------------------------------
# The system never writes a removal.
# ---------------------------------------------------------------------------

def test_remove_never_writes_a_sibling_removal():
    db = FakeDB(_review(_nath_entries()))
    _resolve_duplicate_for_action(db, _acted(END_C, DUR_C), "remove", NOW)
    assert _removal_inserts(db) == []
    assert _removal_updates(db) == []


def test_correct_never_writes_a_sibling_removal():
    db = FakeDB(_review(_same_start_entries()))
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "correct", NOW)
    assert _removal_inserts(db) == []
    assert _removal_updates(db) == []


# ---------------------------------------------------------------------------
# Mixed cluster (nath): removing the runaways leaves two real, non-overlapping
# sessions and BOTH count.
# ---------------------------------------------------------------------------

def test_first_runaway_removed_keeps_group_open_while_other_runaway_bridges():
    db = FakeDB(_review(_nath_entries()))
    _resolve_duplicate_for_action(db, _acted(END_C, DUR_C), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" not in sql
    assert "SET entries" in sql
    assert _labels(args[0]) == ["A", "B", "D"]  # D still overlaps both real sessions


def test_second_runaway_removed_resolves_with_nothing_rejected():
    # C already carries the member's removal from the earlier response.
    db = FakeDB(_review(_nath_entries()),
                removals=[{"end_time": END_C, "duration_min": DUR_C}])
    _resolve_duplicate_for_action(db, _acted(END_D, DUR_D), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
    selected, rejected = args[0], args[1]
    assert selected is None
    assert rejected == []
    assert "resolved_by = 'member'" in sql


def test_both_runaways_in_one_run_resolves_after_the_second():
    # --apply processes the two responses back to back; the second sees the
    # first's removal row and the narrowed snapshot.
    db = FakeDB(_review(_nath_entries()))
    _resolve_duplicate_for_action(db, _acted(END_C, DUR_C), "remove", NOW)
    db.removals.append({"end_time": END_C, "duration_min": DUR_C})
    db.review["entries"] = [e for e in _nath_entries() if e["label"] != "C"]
    _resolve_duplicate_for_action(db, _acted(END_D, DUR_D), "remove", NOW)

    updates = _review_updates(db)
    assert len(updates) == 2
    assert "status = 'resolved'" in updates[1][0]
    assert _removal_inserts(db) == []


# ---------------------------------------------------------------------------
# Same-start drift copies: the member must click every copy; the group stays
# open (reminders continue) until one copy is left.
# ---------------------------------------------------------------------------

def test_same_start_remove_one_of_three_keeps_group_open_with_two_copies():
    db = FakeDB(_review(_same_start_entries()))
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" not in sql
    assert _labels(args[0]) == ["A", "C"]
    assert _removal_inserts(db) == []


def test_same_start_remove_leaving_one_copy_resolves():
    db = FakeDB(_review(_same_start_entries()),
                removals=[{"end_time": S_END_B, "duration_min": S_DUR_B}])
    _resolve_duplicate_for_action(db, _acted(S_END_C, S_DUR_C), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
    assert args[0] is None and args[1] == []


def test_two_way_remove_resolves_and_keeps_the_other_entry():
    db = FakeDB(_review(_same_start_entries()[:2]))
    _resolve_duplicate_for_action(db, _acted(S_END_A, S_DUR_A), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
    assert args[1] == []
    assert _removal_inserts(db) == []


def test_removing_every_copy_resolves_with_no_survivor():
    db = FakeDB(_review(_same_start_entries()[:2]),
                removals=[{"end_time": S_END_A, "duration_min": S_DUR_A}])
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "remove", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
    assert args[0] is None


def test_reverted_removals_are_ignored_server_side():
    db = FakeDB(_review(_same_start_entries()))
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "remove", NOW)
    assert any("REVERTED" in s for s in db.fetched_sql)


# ---------------------------------------------------------------------------
# Edit keeps the edited entry and touches nothing else.
# ---------------------------------------------------------------------------

def test_correct_with_alive_siblings_keeps_group_open():
    db = FakeDB(_review(_same_start_entries()))
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "correct", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" not in sql
    assert _labels(args[0]) == ["A", "B", "C"]


def test_correct_when_siblings_already_removed_resolves_on_the_corrected_entry():
    db = FakeDB(_review(_same_start_entries()),
                removals=[{"end_time": S_END_A, "duration_min": S_DUR_A},
                          {"end_time": S_END_C, "duration_min": S_DUR_C}])
    _resolve_duplicate_for_action(db, _acted(S_END_B, S_DUR_B), "correct", NOW)

    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
    assert args[0] == "B" and args[1] == []


# ---------------------------------------------------------------------------
# Nothing to do cases.
# ---------------------------------------------------------------------------

def test_no_open_group_means_no_writes_even_for_a_legacy_resolved_group():
    # Pre-fix resolved groups keep their state; removing their survivor is just
    # a member removal, no fallback, no revival.
    db = FakeDB(review=None)
    _resolve_duplicate_for_action(db, _acted(S_END_A, S_DUR_A), "remove", NOW)
    assert db.executed == []


def test_entry_outside_the_narrowed_snapshot_changes_nothing():
    db = FakeDB(_review(_same_start_entries()[:2]))
    _resolve_duplicate_for_action(db, _acted(S_END_C, S_DUR_C), "remove", NOW)
    assert db.executed == []


def test_still_running_copies_are_not_clustered():
    entries = _same_start_entries()
    entries[2]["end_time"] = None
    entries[2]["duration_min"] = None
    db = FakeDB(_review(entries))
    _resolve_duplicate_for_action(db, _acted(S_END_A, S_DUR_A), "remove", NOW)

    # B is the only closed alive copy -> nothing overlaps -> resolved.
    (sql, args), = _review_updates(db)
    assert "status = 'resolved'" in sql
