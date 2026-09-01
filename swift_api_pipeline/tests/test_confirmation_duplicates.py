"""Confirmation email: system set-asides are a status, never a member action.

Jamil's rule (2026-09-01): the REMOVED badge belongs only to removals the
member made. A copy the system set aside to keep the clean table unique
('auto_resolved_sibling') shows as DUPLICATE / NOT COUNTED, the surviving
copy of that group is marked COUNTED, totals ignore set-asides, and a note
tells the member how to finish cleaning the group.

Run: python -m pytest tests/test_confirmation_duplicates.py
"""
from datetime import datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import (
    _fetch_classified_day_entries,
    _build_confirmation_summary_html,
    _build_confirmation_entries_html,
    _build_correction_confirmation_html,
)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, sql, *args):
        return self.rows


START = datetime(2026, 8, 17, 12, 53, 19, tzinfo=timezone.utc)
END_A = datetime(2026, 8, 17, 14, 57, 21, tzinfo=timezone.utc)
END_C = datetime(2026, 8, 17, 23, 57, 31, tzinfo=timezone.utc)


def _row(**over):
    base = {
        "project_did": "p1", "project": "TECH-OPS: TS19",
        "user_email": "member@x.co", "start_time": START,
        "end_time": END_A, "duration_min": 124.02,
        "site_name": "SITE", "site_id": None,
        "task": "2. Live Review Complete", "task_clean": "Live Review Complete",
        "corr_orig": None, "is_edited": False, "is_added": False,
        "is_removed": False, "removal_reason": None,
    }
    base.update(over)
    return base


def _classify(rows):
    return _fetch_classified_day_entries(FakeDB(rows), "member@x.co", START.date())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_system_set_aside_classifies_as_duplicate_not_removed():
    out = _classify([_row(is_removed=True, removal_reason="auto_resolved_sibling",
                          end_time=END_C, duration_min=664.20)])
    assert out[0]["status"] == "duplicate"
    assert out[0]["effective_duration_min"] == 0.0


def test_member_removals_still_classify_as_removed():
    out = _classify([
        _row(is_removed=True, removal_reason=None, end_time=END_C, duration_min=664.20),
        _row(is_removed=True, removal_reason="wrong entry", duration_min=539.55),
    ])
    assert [e["status"] for e in out] == ["removed", "removed"]


def test_surviving_copy_of_the_group_is_marked_counted():
    out = _classify([
        _row(),  # surviving real session, overlaps the set-aside copy
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
        _row(site_name="OTHER SITE",
             start_time=datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc),
             end_time=datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc),
             duration_min=60.0),  # unrelated entry, different bucket
    ])
    by_site = {(e.get("site_name"), e["status"]): e for e in out}
    assert by_site[("SITE", "unchanged")].get("counted_in_group") is True
    assert not by_site[("OTHER SITE", "unchanged")].get("counted_in_group")


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def test_summary_ignores_duplicate_copies():
    classified = _classify([
        _row(),
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
    ])
    html = _build_confirmation_summary_html(classified)
    assert "2h 4m" in html           # counted copy only (house _fmt_duration format)
    assert "11h 4m" not in html      # set-aside copy contributes nothing
    assert ">1<" in html             # entries column counts one row


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_duplicate_row_renders_its_own_badge_without_strikethrough():
    classified = _classify([
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
    ])
    html = _build_confirmation_entries_html(classified)
    assert "NOT COUNTED" in html
    assert "line-through" not in html
    assert "REMOVED<" not in html  # never wears the member's badge


def test_counted_row_carries_a_counted_badge():
    classified = _classify([
        _row(),
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
    ])
    html = _build_confirmation_entries_html(classified)
    assert ">COUNTED<" in html  # the counted badge itself, not "NOT COUNTED"


def test_group_note_appears_only_when_set_asides_exist():
    with_dup = _classify([
        _row(),
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
    ])
    without = _classify([_row()])
    body_with = _build_correction_confirmation_html(
        "member@x.co", START.date(), with_dup, 1, 0, 1)
    body_without = _build_correction_confirmation_html(
        "member@x.co", START.date(), without, 1, 0, 1)
    assert "duplicate group" in body_with
    assert "duplicate group" not in body_without


def test_note_for_a_fully_cleared_group_never_mentions_a_counted_copy():
    # Member removed the last live copy themselves: the set-aside still shows
    # as a row, but no COUNTED badge exists anywhere, so the note must explain
    # the group is cleared instead of pointing at a badge that is not there.
    classified = _classify([
        _row(is_removed=True, removal_reason=None),  # member removed the survivor
        _row(is_removed=True, removal_reason="auto_resolved_sibling",
             end_time=END_C, duration_min=664.20),
    ])
    body = _build_correction_confirmation_html(
        "member@x.co", START.date(), classified, 1, 0, 1)
    assert "cleared" in body
    assert "adds to your hours" not in body
