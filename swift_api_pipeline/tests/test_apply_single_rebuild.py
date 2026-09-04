"""run_apply must rebuild stg_timer_activities_clean exactly ONCE per run.

Background (2026-09-04 OntelDB crash loop): every Timer Correction: Apply
dispatch ran rebuild_timer_clean() twice: once inside apply_responses()
when it applied something, then again unconditionally in run_apply(). Each
rebuild is a TRUNCATE + full reload (~60 s, ~1 GB WAL), and 15-23 dispatches
a day made that the single biggest write load on the box.

Contract: apply_responses(rebuild=False) defers to the caller; run_apply()
rebuilds once, after auto-resolve, before confirmation emails.

Run: python -m pytest tests/test_apply_single_rebuild.py -q
"""
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import timer_correction_review as tcr


def _wire(monkeypatch, calls, responses, applied):
    monkeypatch.setattr(tcr, "get_db", lambda: object())
    monkeypatch.setattr(tcr, "read_form_responses", lambda: responses)

    def fake_apply(db, resp, rebuild=True):
        # Mirrors the handler: when it applied something it rebuilds itself
        # unless the caller asked it not to.
        if applied and rebuild:
            tcr.rebuild_clean_table(db)
        return list(applied)

    monkeypatch.setattr(tcr, "apply_responses", fake_apply)
    monkeypatch.setattr(tcr, "auto_resolve_stale", lambda db: calls.append("auto_resolve") or False)
    monkeypatch.setattr(tcr, "rebuild_clean_table", lambda db: calls.append("rebuild"))
    monkeypatch.setattr(
        tcr, "send_correction_confirmations",
        lambda db, changes, test_mode=False: calls.append("confirm"),
    )


def test_apply_responses_accepts_rebuild_kwarg():
    assert "rebuild" in inspect.signature(tcr.apply_responses).parameters


def test_run_apply_rebuilds_once_when_responses_applied(monkeypatch):
    calls = []
    change = {"entry_id": "abc", "action": "remove", "user_email": "m@x.co", "entry_date": None}
    _wire(monkeypatch, calls, responses=[{"entry_id": "abc"}], applied=[change])
    tcr.run_apply()
    assert calls == ["auto_resolve", "rebuild", "confirm"], calls


def test_run_apply_rebuilds_once_when_nothing_to_apply(monkeypatch):
    calls = []
    _wire(monkeypatch, calls, responses=[], applied=[])
    tcr.run_apply()
    assert calls == ["auto_resolve", "rebuild"], calls
