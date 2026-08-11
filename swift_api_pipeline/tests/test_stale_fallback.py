"""Tests for the stale-response fallback helpers in timer_correction_review.py.

A form response carries the entry_id hash computed when the review email was
generated; if the entry changed afterward (running timer completed, re-extract
drift) the hash matches nothing at --apply time and the response used to be
silently skipped. The fallback resolves the response from the form's
"Entry Details" prefill (project | site | task | date | duration) to the
entry's start-key group instead.

Run: python tests/test_stale_fallback.py
"""
from datetime import date, datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import (
    _parse_entry_details, _group_rows, _pick_group,
    _duration_matches, _prefill_entry_id, _strip_entry_id_prefix,
)


# ---------------------------------------------------------------------------
# _parse_entry_details
# ---------------------------------------------------------------------------

def test_parse_standard_details():
    p = _parse_entry_details(
        "TECH-OPS: TS19 | CRESCENT LAKE - New Build | 2. Live Review Complete | Jul 07, 2026 | 0 min")
    assert p is not None
    assert p["project"] == "TECH-OPS: TS19"
    assert p["site"] == "CRESCENT LAKE - New Build"
    assert p["task"] == "2. Live Review Complete"
    assert p["start_date"] == date(2026, 7, 7)


def test_parse_no_site_placeholder_maps_to_none():
    p = _parse_entry_details(
        "TECH-OPS: TS19 | (no site) | 1. General Admin and Support | Jul 15, 2026 | 45h 12m")
    assert p is not None
    assert p["site"] is None
    assert p["task"] == "1. General Admin and Support"
    assert p["start_date"] == date(2026, 7, 15)


def test_parse_site_containing_pipe_joins_middle_parts():
    # A site name containing " | " splits into extra parts; everything between
    # project and task belongs to the site.
    p = _parse_entry_details(
        "TECH-OPS: TS19 | WEIRD | SITE | 2. Site Set-Up Complete | Jul 14, 2026 | 15h 32m")
    assert p is not None
    assert p["site"] == "WEIRD | SITE"
    assert p["task"] == "2. Site Set-Up Complete"


def test_parse_rejects_garbage():
    assert _parse_entry_details("") is None
    assert _parse_entry_details("just some text") is None
    # Unparseable date
    assert _parse_entry_details("P | S | T | not a date | 5 min") is None


# ---------------------------------------------------------------------------
# _group_rows / _pick_group
# ---------------------------------------------------------------------------

def _row(email, start, end_h=None, dur=None, site="S", task="T"):
    return {
        "project_did": "p1", "project": "TECH-OPS: TS19", "user_email": email,
        "start_time": start,
        "site_name": site, "site_id": None, "task": task,
        "end_time": datetime(2026, 7, 7, end_h, tzinfo=timezone.utc) if end_h else None,
        "duration_min": dur,
    }


START_A = datetime(2026, 7, 7, 14, 2, 19, tzinfo=timezone.utc)
START_B = datetime(2026, 7, 7, 16, 0, 0, tzinfo=timezone.utc)


def test_group_rows_groups_by_start_key():
    rows = [
        _row("a@x.co", START_A, 18, 285.41),
        _row("a@x.co", START_A, 23, 11611.54),  # same timer, drifted snapshot
        _row("a@x.co", START_B, 17, 60.0),      # different start = different group
    ]
    groups = _group_rows(rows)
    assert len(groups) == 2
    sizes = sorted(len(g) for g in groups.values())
    assert sizes == [1, 2]


def test_pick_group_single_group_wins_regardless_of_respondent():
    groups = _group_rows([_row("member@x.co", START_A, 18, 100.0)])
    picked = _pick_group(groups, "someoneelse@x.co")
    assert picked is not None
    assert picked[0]["user_email"] == "member@x.co"


def test_pick_group_ambiguous_resolved_by_respondent_email():
    groups = _group_rows([
        _row("a@x.co", START_A, 18, 100.0),
        _row("b@x.co", START_B, 18, 100.0),
    ])
    picked = _pick_group(groups, "b@x.co")
    assert picked is not None
    assert picked[0]["user_email"] == "b@x.co"


def test_pick_group_ambiguous_without_respondent_match_returns_none():
    groups = _group_rows([
        _row("a@x.co", START_A, 18, 100.0),
        _row("b@x.co", START_B, 18, 100.0),
    ])
    assert _pick_group(groups, "c@x.co") is None
    assert _pick_group(groups, None) is None


def test_pick_group_two_groups_same_member_is_ambiguous():
    # Same member, same site+task, two separate timers that day: cannot tell
    # which one the response meant.
    groups = _group_rows([
        _row("a@x.co", START_A, 18, 100.0),
        _row("a@x.co", START_B, 19, 50.0),
    ])
    assert _pick_group(groups, "a@x.co") is None


# ---------------------------------------------------------------------------
# _parse_entry_details duration_str + _duration_matches
# (guards the 2026-08-10 over-removal incident: a stale removal must target
# only the snapshot the member saw, never the whole start-key group)
# ---------------------------------------------------------------------------

def test_parse_details_carries_duration_str():
    p = _parse_entry_details(
        "TECH-OPS: TS19 | D-HBR237 | 6. Final COP Complete | Jun 22, 2026 | 0 min")
    assert p["duration_str"] == "0 min"
    p = _parse_entry_details(
        "TECH-OPS: TS19 | (no site) | 1. General Admin and Support | Jul 15, 2026 | 45h 12m")
    assert p["duration_str"] == "45h 12m"


def test_duration_matches_same_formatter_output():
    assert _duration_matches("0 min", 0.28)          # sub-minute ghost rounds to 0 min
    assert _duration_matches("8h 18m", 498.46)
    assert _duration_matches("22h 50m", 1370.3)
    assert _duration_matches("45 min", 44.9)


def test_duration_matches_rejects_drifted_row():
    # The Manalac case: member saw a 0-min running snapshot; at apply time the
    # row is a real 8.3h session. The removal must NOT match it.
    assert not _duration_matches("0 min", 498.46)
    assert not _duration_matches("22h 50m", 498.46)
    assert not _duration_matches(None, 498.46)
    assert not _duration_matches("", 498.46)


# ---------------------------------------------------------------------------
# entry-id sentinel prefix (Google Sheets scientific-notation mangling guard)
# ---------------------------------------------------------------------------

def test_prefill_roundtrip():
    assert _strip_entry_id_prefix(_prefill_entry_id("539e17ab12cd")) == "539e17ab12cd"


def test_strip_accepts_legacy_bare_ids():
    assert _strip_entry_id_prefix("6b7c45ddd18a") == "6b7c45ddd18a"
    assert _strip_entry_id_prefix("  6b7c45ddd18a  ") == "6b7c45ddd18a"
    assert _strip_entry_id_prefix("") == ""


def test_strip_handles_prefix_case_and_whitespace():
    assert _strip_entry_id_prefix(" ID:6b7c45ddd18a ") == "6b7c45ddd18a"
    assert _strip_entry_id_prefix("id: 6b7c45ddd18a") == "6b7c45ddd18a"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if not failed else 1)
