"""Confirmation email: every live entry carries its own Edit / Remove buttons.

Real case (abbie 2026-10-12): Swift emitted a 19h22m never-stopped copy of a
07:26 session. It was still running at the 18:30 ET daily send (no buttons,
running notice only), and rebuild Step 5 kept it out of clean while the
member had not touched the group. Her edit of the real session made the group
member-touched, so the runaway came back into clean and showed up for the
first time in the "Timer Entries Updated" confirmation, a table with no
Action column. The member could see it but had no way to remove it.

Rule: rows that still count (unchanged / edited / added, settled) get the same
Edit / Remove buttons as the daily email; removed rows, system set-asides and
running timers get none.

Run: python -m pytest tests/test_confirmation_buttons.py
"""
from datetime import datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import (
    _fetch_classified_day_entries,
    _build_confirmation_entries_html,
    _build_correction_confirmation_html,
    _make_entry_id,
)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, sql, *args):
        return self.rows


START = datetime(2026, 10, 12, 11, 26, 7, tzinfo=timezone.utc)
END_REAL = datetime(2026, 10, 12, 21, 31, 7, tzinfo=timezone.utc)
END_RUNAWAY = datetime(2026, 10, 13, 6, 48, 48, tzinfo=timezone.utc)


def _row(**over):
    base = {
        "project_did": "p1", "project": "TECH-OPS: TS20",
        "user_email": "member@x.co", "start_time": START,
        "end_time": END_RUNAWAY, "duration_min": 1162.69,
        "site_name": None, "site_id": None,
        "task": "1. General Admin and Support", "task_clean": "General Admin and Support",
        "corr_orig": None, "is_edited": False, "is_added": False,
        "is_removed": False, "removal_reason": None,
    }
    base.update(over)
    return base


def _classify(rows):
    return _fetch_classified_day_entries(FakeDB(rows), "member@x.co", START.date(), "first")


def _row_html(html, n):
    """The n-th <tr> of the rendered body (0-based)."""
    return html.split("<tr")[n + 1]


def test_unchanged_runaway_gets_edit_and_remove_buttons():
    entries = _classify([_row()])
    html = _build_confirmation_entries_html(entries)
    eid = _make_entry_id("p1", "member@x.co", START, None, None,
                         "1. General Admin and Support", END_RUNAWAY, 1162.69)
    assert ">Edit</a>" in html
    assert ">Remove</a>" in html
    assert eid in html


def test_edited_row_gets_buttons():
    entries = _classify([_row(end_time=END_REAL, duration_min=605,
                              is_edited=True, corr_orig=566.12)])
    html = _build_confirmation_entries_html(entries)
    assert ">Edit</a>" in html and ">Remove</a>" in html


def test_removed_and_set_aside_rows_get_no_buttons():
    entries = _classify([
        _row(is_removed=True, removal_reason="Forgot to stop timer"),
        _row(end_time=END_REAL, duration_min=605, is_removed=True,
             removal_reason="auto_resolved_sibling"),
    ])
    html = _build_confirmation_entries_html(entries)
    assert ">Edit</a>" not in html
    assert ">Remove</a>" not in html


def test_running_row_gets_no_buttons():
    entries = _classify([_row(end_time=None, duration_min=0)])
    html = _build_confirmation_entries_html(entries)
    assert ">Edit</a>" not in html


def test_every_row_has_eight_cells_to_match_the_action_header():
    entries = _classify([
        _row(),
        _row(end_time=END_REAL, duration_min=605, is_edited=True, corr_orig=566.12),
        _row(is_removed=True, removal_reason="Forgot to stop timer"),
        _row(end_time=None, duration_min=0),
    ])
    html = _build_confirmation_entries_html(entries)
    for i in range(4):
        assert _row_html(html, i).count("<td") == 8, i


def test_full_email_has_action_header_and_points_to_this_email():
    entries = _classify([_row()])
    html = _build_correction_confirmation_html(
        "member@x.co", START.date(), entries, "first",
        change_count=1, edit_count=1, removal_count=0)
    assert ">Action</th>" in html
    assert "in <strong>this</strong> email" in html
