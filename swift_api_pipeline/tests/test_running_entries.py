"""Tests for the still-running-timer handling in timer_correction_review.py.

Running timers (end_time IS NULL) are no longer actionable rows in the daily
email; they are split out and surfaced in a "timer still running" notice with
no Edit / Remove buttons. Ghost rows (NULL end_time with a completed sibling on
the same start-key) are dropped silently.

Run: python -m pytest tests/test_running_entries.py
"""
from datetime import datetime, timedelta, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import (  # noqa: E402
    _split_running_entries, _build_running_notice_html, _collect_entry_ids,
    _build_entries_html,
)

T0 = datetime(2026, 8, 23, 18, 5, 35, tzinfo=timezone.utc)  # 2:05 PM ET


def _e(start=T0, end="auto", minutes=30, task="6. Final COP Complete",
       site="BASSETT FORKS - 5G L-Sub6 - Carrier Add", site_id="sid-1",
       project_did="-Omzv", user="prince@ontel.co", asset_did="-OwTP"):
    if end == "auto":
        end = start + timedelta(minutes=minutes)
    return {
        "project": "TECH-OPS: TS19", "project_did": project_did,
        "user_email": user, "start_time": start, "end_time": end,
        "duration_min": minutes if end is not None else 0,
        "site_name": site, "site_id": site_id, "task": task,
        "task_clean": task.split(". ", 1)[-1], "asset_did": asset_did,
    }


# ---------------------------------------------------------------------------
# _split_running_entries
# ---------------------------------------------------------------------------

def test_split_all_settled_passthrough():
    entries = [_e(), _e(start=T0 + timedelta(hours=2), task="7. Training")]
    settled, running = _split_running_entries(entries)
    assert settled == entries
    assert running == []


def test_split_running_timer_leaves_table():
    run = _e(start=T0 + timedelta(hours=3), end=None, task="1. General Admin and Support",
             site=None, site_id=None, asset_did=None)
    settled, running = _split_running_entries([_e(), run])
    assert settled == [_e()]
    assert running == [run]


def test_split_drops_ghost_with_completed_sibling():
    # Same start-key: one stale NULL-end snapshot next to the completed row.
    ghost = _e(end=None)
    real = _e(minutes=131)
    settled, running = _split_running_entries([ghost, real])
    assert settled == [real]
    assert running == [], "a NULL-end row with a completed sibling is not running"


def test_split_dedupes_running_by_start_key():
    a = _e(end=None)
    b = _e(end=None)
    settled, running = _split_running_entries([a, b])
    assert settled == []
    assert len(running) == 1


def test_split_preserves_order_of_settled_rows():
    e1 = _e(start=T0, task="2. Live Review Complete")
    e2 = _e(start=T0 + timedelta(hours=1), end=None)
    e3 = _e(start=T0 + timedelta(hours=2), task="7. Training")
    settled, running = _split_running_entries([e1, e2, e3])
    assert settled == [e1, e3]
    assert running == [e2]


def test_split_empty():
    assert _split_running_entries([]) == ([], [])


# ---------------------------------------------------------------------------
# _build_running_notice_html
# ---------------------------------------------------------------------------

def test_notice_empty_when_nothing_running():
    assert _build_running_notice_html([]) == ""


def test_notice_lists_site_task_and_et_start_with_no_buttons():
    run = _e(end=None)
    now = T0 + timedelta(hours=5, minutes=20)
    html = _build_running_notice_html([run], now=now)
    assert "BASSETT FORKS" in html
    assert "6. Final COP Complete" in html
    assert "2:05 PM" in html            # ET clock time of the start
    assert "5h 20m" in html             # elapsed as of `now`
    assert "swiftprojects.io" in html
    # No action buttons: the only link is the Swift one, no form URLs.
    assert html.count("<a ") == 1
    assert "docs.google.com/forms" not in html and "viewform" not in html


def test_notice_handles_no_site_and_escapes_html():
    run = _e(end=None, site=None, site_id=None, asset_did=None,
             task="1. General <Admin> & Support")
    html = _build_running_notice_html([run], now=T0 + timedelta(minutes=90))
    assert "(no site)" in html
    assert "&lt;Admin&gt; &amp; Support" in html
    assert "<Admin>" not in html
    assert "1h 30m" in html


def test_notice_mentions_follow_up_on_thread():
    html = _build_running_notice_html([_e(end=None)], now=T0 + timedelta(hours=1))
    assert "follow-up" in html.lower()


# ---------------------------------------------------------------------------
# Snapshot + table interplay
# ---------------------------------------------------------------------------

def test_snapshot_of_settled_excludes_running_key():
    run = _e(end=None)
    settled, running = _split_running_entries([_e(), run])
    ids = _collect_entry_ids(settled)
    assert len(ids) == 1
    # Once the timer completes its key differs from the running key, so the
    # completed row is NEW relative to a settled-only snapshot.
    completed = _e(minutes=131)
    assert _collect_entry_ids([completed])[0] not in ids


def test_entries_table_has_no_running_rows_after_split():
    run = _e(end=None)
    settled, _ = _split_running_entries([_e(), run])
    html = _build_entries_html(settled)
    assert html.count("<tr") == 1


# ---------------------------------------------------------------------------
# Resend trigger math (find_days_needing_resend) with the DB stubbed out
# ---------------------------------------------------------------------------

def test_resend_trigger_ignores_running_timer_and_fires_on_completion(monkeypatch):
    import timer_correction_review as tcr
    from datetime import date

    settled_a = _e()
    running_b = _e(start=T0 + timedelta(hours=3), end=None, task="7. Training")
    completed_b = _e(start=T0 + timedelta(hours=3), minutes=95, task="7. Training")
    for x in (settled_a, running_b, completed_b):
        x["is_edited"] = False

    rows = [{
        "user_email": settled_a["user_email"], "send_date": date(2026, 8, 23),
        "thread_id": "thread-1", "message_id": "<m@x>", "last_sent_at": None,
        "last_sent_entry_ids": _collect_entry_ids([settled_a]),  # settled-only snapshot
    }]
    writes = []

    def fake_retry_db(fn, description=""):
        if "candidates" in description:
            return rows
        writes.append(description)
        return None

    monkeypatch.setattr(tcr, "retry_db", fake_retry_db)

    current = [settled_a, running_b]
    monkeypatch.setattr(tcr, "_fetch_current_day_entries", lambda db, u, d: list(current))
    assert tcr.find_days_needing_resend(None) == [], "a still-running timer must not trigger a resend"
    assert writes == [], "no bootstrap write when a snapshot already exists"

    current[:] = [settled_a, completed_b]
    cands = tcr.find_days_needing_resend(None)
    assert len(cands) == 1
    assert cands[0]["current_entries"] == [settled_a, completed_b]
    assert tcr._resend_has_new_rows(
        _split_running_entries(cands[0]["current_entries"])[0], cands[0]["snapshot_ids"])


def test_resend_bootstrap_snapshot_is_settled_only(monkeypatch):
    import timer_correction_review as tcr
    from datetime import date

    settled_a = _e(); settled_a["is_edited"] = False
    running_b = _e(start=T0 + timedelta(hours=3), end=None); running_b["is_edited"] = False
    rows = [{
        "user_email": settled_a["user_email"], "send_date": date(2026, 8, 23),
        "thread_id": "thread-1", "message_id": None, "last_sent_at": None,
        "last_sent_entry_ids": None,  # never snapshotted -> bootstrap path
    }]
    captured = {}

    def fake_retry_db(fn, description=""):
        if "candidates" in description:
            return rows
        captured["description"] = description
        return None

    monkeypatch.setattr(tcr, "retry_db", fake_retry_db)
    monkeypatch.setattr(tcr, "_fetch_current_day_entries", lambda db, u, d: [settled_a, running_b])
    assert tcr.find_days_needing_resend(None) == []
    assert "bootstrap" in captured.get("description", "")
