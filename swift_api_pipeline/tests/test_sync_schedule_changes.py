"""Tests for sync_schedule_changes: identity resolution, wipe guard, dry-run.

DB is faked with the house duck-typed stub style (no unittest.mock, no network).
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_changes_source import ParsedRow  # noqa: E402
from sync_schedule_changes import (  # noqa: E402
    guard_says_abort,
    resolve_emp_ids,
)


def _row(id_number="250901", name="Jamil Mendez", tab="DA", start=date(2025, 9, 22)):
    return ParsedRow(
        sheet_tab=tab, row_index=3, id_number=id_number, member_name=name,
        role="DA", shift_start_pht="1 PM", shift_end_pht="10 PM",
        shift_start_et="1 AM", shift_end_et="10 AM", shift_code="DS",
        work_arrangement="5DWW", reg_hours=9, rest_day=None, rdo_to=None,
        rdo_day=None, start_date=start, end_date=None, change_kind="ongoing",
        notes=None, raw_cells=["x"], row_hash="h")


ROSTER = [
    {"emp_id": "250901", "full_name": "Jamil Ryan Mendez", "first_name": "Jamil",
     "nickname": "Jamil"},
    {"emp_id": "220209", "full_name": "Hajie Valleser", "first_name": "Hajie",
     "nickname": "Hajie"},
    {"emp_id": "999901", "full_name": "Maria Santos", "first_name": "Maria",
     "nickname": "Maria"},
    {"emp_id": "999902", "full_name": "Maria Reyes Santos", "first_name": "Marie",
     "nickname": "Marie"},
]


class FakeDb:
    def __init__(self, fetch_rows=None):
        self.fetch_rows = fetch_rows if fetch_rows is not None else ROSTER
        self.executed = []

    def fetch(self, q, *a):
        return self.fetch_rows

    def fetchval(self, q, *a):
        return 100

    def execute(self, q, *a):
        self.executed.append(" ".join(q.split()))

    def executemany(self, q, rows):
        self.executed.append((" ".join(q.split()), len(rows)))


def test_resolve_by_id_number():
    resolved, skips = resolve_emp_ids(FakeDb(), [_row()])
    assert skips == []
    assert resolved[0][0] == "250901"


def test_resolve_unknown_id_passes_through():
    # Roster misses some old members; the sheet id is the same keyspace, keep it.
    resolved, skips = resolve_emp_ids(FakeDb(), [_row(id_number="180901")])
    assert resolved[0][0] == "180901"
    assert skips == []


def test_resolve_blank_id_by_unique_name():
    resolved, skips = resolve_emp_ids(
        FakeDb(), [_row(id_number="", name="Hajie Valleser")])
    assert skips == []
    assert resolved[0][0] == "220209"


def test_resolve_blank_id_first_last_token_match():
    # "Jamil Mendez" matches full_name "Jamil Ryan Mendez" on first+last token.
    resolved, skips = resolve_emp_ids(
        FakeDb(), [_row(id_number="", name="Jamil Mendez")])
    assert skips == []
    assert resolved[0][0] == "250901"


def test_resolve_blank_id_ambiguous_name_is_skipped():
    # Two roster members share first token "Maria" + last token "Santos".
    resolved, skips = resolve_emp_ids(
        FakeDb(), [_row(id_number="", name="Maria Santos")])
    assert resolved == []
    assert len(skips) == 1 and "ambiguous" in skips[0]


def test_resolve_blank_id_unmatched_name_is_skipped():
    resolved, skips = resolve_emp_ids(
        FakeDb(), [_row(id_number="", name="Nobody Here")])
    assert resolved == []
    assert len(skips) == 1 and "unmatched" in skips[0]


def test_guard_aborts_on_big_shrink():
    assert guard_says_abort(prev_count=100, new_count=40) is True


def test_guard_allows_growth_and_small_tables():
    assert guard_says_abort(prev_count=100, new_count=95) is False
    assert guard_says_abort(prev_count=700, new_count=710) is False
    # Tiny previous table: guard stands down (first loads, test data).
    assert guard_says_abort(prev_count=10, new_count=1) is False
    assert guard_says_abort(prev_count=0, new_count=0) is False
