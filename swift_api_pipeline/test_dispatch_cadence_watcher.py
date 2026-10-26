# swift_api_pipeline/test_dispatch_cadence_watcher.py
import json
from datetime import datetime, timedelta, timezone

from dispatch_cadence_watcher import (
    ANY_DISPATCH,
    build_email,
    decide,
    evaluate,
    load_state,
    parse_ts,
    save_state,
)

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
DR = "Pipeline: Daily Reports (Rolling)"
UP = "Pipeline: User Priorities"


def newest(any_min, dr_min, up_min):
    def ts(m):
        return None if m is None else NOW - timedelta(minutes=m)
    return {ANY_DISPATCH: ts(any_min), DR: ts(dr_min), UP: ts(up_min)}


# --- evaluate --------------------------------------------------------------

def test_parse_ts_handles_github_z_suffix():
    assert parse_ts("2026-10-26T05:52:59Z") == datetime(2026, 10, 26, 5, 52, 59, tzinfo=timezone.utc)
    assert parse_ts(None) is None


def test_normal_cadence_is_green():
    # Worst normal gaps seen in 30 days: 5.6 min overall, 10 min per heartbeat.
    ages, breaches = evaluate(newest(5.6, 10, 10), NOW)
    assert breaches == []
    assert round(ages[ANY_DISPATCH], 1) == 5.6


def test_threshold_is_strict():
    assert evaluate(newest(30, 30, 30), NOW)[1] == []
    assert evaluate(newest(30.1, 1, 1), NOW)[1] == [ANY_DISPATCH]


def test_full_outage_breaches_every_check():
    # 2026-09-30 shape: the PAT died everywhere at once.
    _, breaches = evaluate(newest(1330, 1330, 1334), NOW)
    assert breaches == sorted([ANY_DISPATCH, DR, UP])


def test_partial_outage_caught_by_heartbeat_only():
    # One Apps Script project's token dead, the other still dispatching:
    # the any-dispatch check stays green but the heartbeat does not.
    _, breaches = evaluate(newest(2, 95, 2), NOW)
    assert breaches == [DR]


def test_no_runs_at_all_is_a_breach():
    ages, breaches = evaluate(newest(None, 3, 3), NOW)
    assert breaches == [ANY_DISPATCH] and ages[ANY_DISPATCH] is None


def test_custom_threshold():
    assert evaluate(newest(12, 1, 1), NOW, threshold_min=10)[1] == [ANY_DISPATCH]


# --- decide (no email storm) -----------------------------------------------

def test_healthy_without_state_does_nothing():
    action, state = decide({}, [], NOW)
    assert action == "none" and state == {}


def test_first_breach_alerts_once_then_stays_quiet():
    action, s1 = decide({}, [ANY_DISPATCH], NOW)
    assert action == "alert" and s1["alerting"] and s1["last_email_at"] == NOW.isoformat()
    for minutes in (15, 30, 60, 5 * 60 + 59):
        action, s = decide(s1, [ANY_DISPATCH], NOW + timedelta(minutes=minutes))
        assert action == "none"
        assert s["last_email_at"] == s1["last_email_at"]


def test_reminder_after_six_hours():
    _, s1 = decide({}, [ANY_DISPATCH], NOW)
    later = NOW + timedelta(hours=6)
    action, s2 = decide(s1, [ANY_DISPATCH], later)
    assert action == "remind" and s2["last_email_at"] == later.isoformat()
    action, _ = decide(s2, [ANY_DISPATCH], later + timedelta(minutes=15))
    assert action == "none"


def test_new_check_joining_the_breach_alerts_again():
    _, s1 = decide({}, [DR], NOW)
    action, s2 = decide(s1, sorted([ANY_DISPATCH, DR, UP]), NOW + timedelta(minutes=15))
    assert action == "alert"
    # Shrinking (partial recovery) is quiet, and keeps the alerting state.
    action, s3 = decide(s2, [DR], NOW + timedelta(minutes=30))
    assert action == "none" and s3["alerting"] and s3["breached"] == [DR]


def test_recovery_sends_one_email_then_silence():
    _, s1 = decide({}, [ANY_DISPATCH], NOW)
    action, s2 = decide(s1, [], NOW + timedelta(hours=1))
    assert action == "recover" and s2["alerting"] is False
    action, s3 = decide(s2, [], NOW + timedelta(hours=1, minutes=15))
    assert action == "none" and s3 == s2


def test_breach_after_recovery_alerts_again():
    _, s1 = decide({}, [ANY_DISPATCH], NOW)
    _, s2 = decide(s1, [], NOW + timedelta(hours=1))
    action, _ = decide(s2, [ANY_DISPATCH], NOW + timedelta(hours=1, minutes=15))
    assert action == "alert"


# --- email + state file ----------------------------------------------------

def test_alert_email_names_breaches_and_converts_to_et():
    nw = newest(1330, 1330, 2)
    ages, breaches = evaluate(nw, NOW)
    subject, body = build_email("alert", ages, nw, breaches, NOW)
    assert "STOPPED" in subject and "2 check(s)" in subject
    assert "[BREACHED] Any dispatch-triggered workflow" in body
    assert f"[ok] {UP}" in body
    assert "22.2 hours ago" in body
    assert "08:00 AM ET" in body  # 12:00 UTC on 2026-10-26 = 08:00 EDT
    assert "GITHUB_TOKEN" in body


def test_recovery_email_has_no_runbook():
    nw = newest(2, 2, 2)
    ages, breaches = evaluate(nw, NOW)
    subject, body = build_email("recover", ages, nw, breaches, NOW)
    assert "recovered" in subject and "RECOVERED" in body
    assert "Rotation map" not in body


def test_state_round_trip_and_missing_or_corrupt_file(tmp_path):
    p = tmp_path / "sub" / "state.json"
    assert load_state(str(p)) == {}
    state = {"alerting": True, "breached": [DR], "last_email_at": NOW.isoformat()}
    save_state(str(p), state)
    assert load_state(str(p)) == state
    p.write_text("{not json", encoding="utf-8")
    assert load_state(str(p)) == {}
    assert load_state(None) == {}
    json.dumps(state)  # state stays JSON-serialisable
