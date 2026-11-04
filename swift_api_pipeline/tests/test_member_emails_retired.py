"""The Timer Activity Entries email is retired (2026-11-04): My Timers replaces it.

What stops: every email to a member from timer_correction_review.py
  --send     the daily Timer Activity Entries email
  --apply    the reply-in-thread confirmation after a form fix
  --remind   the duplicate reminder
  --resend   the threaded re-send of a changed day

What keeps running, because the reports still read stg_timer_activities_clean:
  --send     duplicate tracking (the clean table counts a duplicate once, and
             auto-resolve keeps the longest after 7 days)
  --apply    form responses (old emails' buttons still work), auto-resolve, rebuild

TIMER_MEMBER_EMAILS=on turns the emails back on (emergency only).

Run: python -m pytest tests/test_member_emails_retired.py -q
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import timer_correction_review as tcr


def _boom(name):
    def f(*a, **k):
        raise AssertionError(f"{name} must not run while the member emails are retired")
    return f


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("TIMER_MEMBER_EMAILS", raising=False)
    assert tcr.member_emails_on() is False


def test_on_only_when_asked(monkeypatch):
    for value, expected in [("on", True), (" ON ", True), ("off", False), ("", False), ("yes", False)]:
        monkeypatch.setenv("TIMER_MEMBER_EMAILS", value)
        assert tcr.member_emails_on() is expected, value


def test_send_tracks_duplicates_without_emailing(monkeypatch):
    monkeypatch.delenv("TIMER_MEMBER_EMAILS", raising=False)
    calls = []
    entries = [{"user_email": "m@x.co"}]
    monkeypatch.setattr(tcr, "get_db", lambda: object())
    monkeypatch.setattr(tcr, "get_previous_day_entries", lambda db, target_date=None, part=None: entries)
    monkeypatch.setattr(tcr, "send_daily_emails", _boom("send_daily_emails"))
    monkeypatch.setattr(tcr, "detect_and_track_duplicates", lambda db, e: calls.append(("track", len(e))))
    tcr.run_send(target_date=tcr.date(2026, 11, 4), part="first")
    assert calls == [("track", 1)]


def test_send_emails_again_when_switched_on(monkeypatch):
    monkeypatch.setenv("TIMER_MEMBER_EMAILS", "on")
    calls = []
    monkeypatch.setattr(tcr, "get_db", lambda: object())
    monkeypatch.setattr(tcr, "get_previous_day_entries",
                        lambda db, target_date=None, part=None: [{"user_email": "m@x.co"}])
    monkeypatch.setattr(tcr, "send_daily_emails", lambda db, e, **k: calls.append("send"))
    monkeypatch.setattr(tcr, "detect_and_track_duplicates", lambda db, e: calls.append("track"))
    tcr.run_send(target_date=tcr.date(2026, 11, 4), part="first")
    assert calls == ["send", "track"]


def test_apply_still_applies_and_rebuilds_but_sends_no_confirmation(monkeypatch):
    monkeypatch.delenv("TIMER_MEMBER_EMAILS", raising=False)
    calls = []
    change = {"entry_id": "abc", "action": "remove", "user_email": "m@x.co", "entry_date": None}
    monkeypatch.setattr(tcr, "get_db", lambda: object())
    monkeypatch.setattr(tcr, "read_form_responses", lambda: [{"entry_id": "abc"}])
    monkeypatch.setattr(tcr, "apply_responses",
                        lambda db, r, rebuild=True: calls.append("apply") or [change])
    monkeypatch.setattr(tcr, "auto_resolve_stale", lambda db: calls.append("auto_resolve") or False)
    monkeypatch.setattr(tcr, "rebuild_clean_table", lambda db: calls.append("rebuild"))
    monkeypatch.setattr(tcr, "send_correction_confirmations", _boom("send_correction_confirmations"))
    tcr.run_apply()
    assert calls == ["apply", "auto_resolve", "rebuild"]


def test_remind_sends_nothing(monkeypatch):
    monkeypatch.delenv("TIMER_MEMBER_EMAILS", raising=False)
    monkeypatch.setattr(tcr, "get_db", _boom("get_db"))
    tcr.run_remind()


def test_resend_sends_nothing(monkeypatch):
    monkeypatch.delenv("TIMER_MEMBER_EMAILS", raising=False)
    monkeypatch.setattr(tcr, "get_db", _boom("get_db"))
    monkeypatch.setattr(tcr, "send_resend_emails", _boom("send_resend_emails"))
    tcr.run_resend()
