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


# ---------------------------------------------------------------------------
# Swift deep link per running timer (DRMC pattern)
# ---------------------------------------------------------------------------

def test_notice_links_task_when_did_known_else_root():
    from timer_correction_review import _task_link_key
    on_asset = _e(end=None)                                   # asset_did "-OwTP"
    admin = _e(start=T0 + timedelta(hours=2), end=None, site=None, site_id=None,
               asset_did=None, task="1. General Admin and Support")
    dids = {_task_link_key(on_asset): "-OTaskDid123"}
    html = _build_running_notice_html([on_asset, admin], now=T0 + timedelta(hours=3), task_dids=dids)
    assert "https://swiftprojects.io/#/app/assets/tasks/-OTaskDid123/requirements" in html
    assert "Open in Swift" in html
    assert html.count("<a ") == 2
    assert 'href="https://swiftprojects.io/"' in html        # admin timer: root link
    assert "docs.google.com/forms" not in html


def test_task_link_key_requires_asset_and_task():
    from timer_correction_review import _task_link_key
    assert _task_link_key(_e(asset_did=None)) is None
    assert _task_link_key(_e(task="")) is None
    assert _task_link_key(_e(task=" 6. Final COP Complete ")) == ("-OwTP", "6. Final COP Complete")


def test_lookup_task_dids_batches_unique_keys_and_skips_no_asset(monkeypatch):
    import timer_correction_review as tcr
    seen = {}

    def fake_retry_db(fn, description=""):
        seen["description"] = description
        return [{"asset_did": "-OwTP", "task": "6. Final COP Complete", "task_did": "-OTask"}]

    monkeypatch.setattr(tcr, "retry_db", fake_retry_db)
    entries = [_e(end=None), _e(end=None),                       # same key twice
               _e(end=None, site=None, site_id=None, asset_did=None,
                  task="1. General Admin and Support")]           # no asset
    out = tcr._lookup_task_dids(None, entries)
    assert out == {("-OwTP", "6. Final COP Complete"): "-OTask"}
    assert "1 running timers" in seen["description"]


def test_lookup_task_dids_empty_without_assets(monkeypatch):
    import timer_correction_review as tcr
    monkeypatch.setattr(tcr, "retry_db", lambda fn, description="": (_ for _ in ()).throw(AssertionError("no query expected")))
    assert tcr._lookup_task_dids(None, [_e(end=None, asset_did=None)]) == {}
    assert tcr._lookup_task_dids(None, []) == {}


def test_safe_task_dids_degrades_to_root_links(monkeypatch):
    import timer_correction_review as tcr

    def boom(fn, description=""):
        raise RuntimeError("stg_asset_tasks unavailable")

    monkeypatch.setattr(tcr, "retry_db", boom)
    assert tcr._safe_task_dids(None, [_e(end=None)]) == {}
    assert tcr._safe_task_dids(None, []) == {}


def test_daily_send_survives_task_lookup_failure(monkeypatch):
    """A DB hiccup resolving Swift task DIDs must not abort the send loop."""
    import timer_correction_review as tcr
    import gmail_client

    sent = []

    class _Exec:
        def __init__(self, p): self._p = p
        def execute(self): return self._p

    class _Msgs:
        def send(self, userId, body):
            sent.append(body["raw"]); return _Exec({"threadId": "t", "id": "m"})
        def get(self, **kw): return _Exec({"payload": {"headers": []}})

    class _Svc:
        def users(self): return self
        def messages(self): return _Msgs()

    monkeypatch.setattr(gmail_client, "authenticate", lambda: _Svc())
    monkeypatch.setattr(gmail_client, "masked_sender", lambda s, n: "x <x@ontel.co>")
    monkeypatch.setattr(tcr, "retry_db", lambda fn, description="": None)

    def boom(db, entries):
        raise RuntimeError("stg_asset_tasks unavailable")
    monkeypatch.setattr(tcr, "_lookup_task_dids", boom)

    from datetime import date
    entries = [_e(), _e(start=T0 + timedelta(hours=3), end=None),
               _e(user="other@ontel.co"), _e(user="other@ontel.co", start=T0 + timedelta(hours=1), end=None)]
    tcr.send_daily_emails(None, entries, test_mode=True, target_date=date(2026, 8, 23))
    assert len(sent) == 2, "both techs' emails still go out when the deep-link lookup fails"
