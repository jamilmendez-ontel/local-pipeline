"""Tests for should_fetch_timers() in extract_daily_reports.py.

Members clock in (start the attendance timer) on a DR task that is still
'pending' with no requirement rows — the timer is the first thing that exists
on the task. The old inline filter skipped timer fetch for pending/0-req
tasks entirely, so a clock-in stayed invisible to DR Monitoring until the
task left 'pending' (Jamil Mendez 2026-08-11: clock-in 9:08 AM PHT, invisible
until ~4:33 PM PHT). Pending tasks within a short work-date lookback must be
fetched; older pending tasks must stay excluded because DR tasks are
pre-created months ahead (~17.8k pending rows in a 30-day window vs ~140
within the lookback).

Run: python tests/test_dr_timer_fetch_policy.py
"""
from datetime import date, timedelta
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from extract_daily_reports import PENDING_TIMER_LOOKBACK_DAYS, should_fetch_timers

TODAY = date(2026, 8, 11)

CASES = [
    # (status, req_count, work_date, expected, label)
    ("in_progress", 0, TODAY, True, "in_progress always fetched"),
    ("submitted", 0, TODAY - timedelta(days=10), True, "submitted always fetched"),
    ("approved", 0, TODAY - timedelta(days=25), True, "approved always fetched"),
    ("pending", 3, TODAY - timedelta(days=20), True, "pending with reqs fetched (old rule kept)"),
    ("cancelled", 2, TODAY, True, "cancelled with reqs fetched (old rule kept)"),
    ("cancelled", 0, TODAY, False, "cancelled without reqs never fetched"),
    # The bug: clock-in exists on a pending 0-req DR dated today.
    ("pending", 0, TODAY, True, "pending 0-req TODAY fetched (Jamil 2026-08-11)"),
    ("pending", 0, TODAY - timedelta(days=PENDING_TIMER_LOOKBACK_DAYS), True,
     "pending 0-req at lookback edge fetched"),
    ("pending", 0, TODAY - timedelta(days=PENDING_TIMER_LOOKBACK_DAYS + 1), False,
     "pending 0-req beyond lookback excluded"),
    ("pending", 0, TODAY - timedelta(days=20), False, "old pending 0-req excluded"),
    ("pending", 0, None, False, "pending 0-req with unparseable work date excluded"),
]


def main():
    failures = 0
    for status, req_count, work_date, expected, label in CASES:
        got = should_fetch_timers(status, req_count, work_date, TODAY)
        ok = got is expected
        print(f"{'PASS' if ok else 'FAIL'}: {label} (got {got}, want {expected})")
        failures += 0 if ok else 1
    if failures:
        print(f"\n{failures} FAILED")
        sys.exit(1)
    print(f"\nAll {len(CASES)} cases passed")


if __name__ == "__main__":
    main()
