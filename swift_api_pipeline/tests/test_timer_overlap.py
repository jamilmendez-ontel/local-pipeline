"""Tests for timer overlap detection helpers in timer_correction_review.py.

Run: python tests/test_timer_overlap.py
"""
from datetime import datetime, timezone
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import _intervals_overlap


def _ts(h, m=0):
    """Build a UTC timestamp on a fixed date with given hour/minute."""
    return datetime(2026, 5, 7, h, m, tzinfo=timezone.utc)


def test_intervals_overlap_identical():
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(9, 30), _ts(11, 30)) is True


def test_intervals_overlap_contained():
    # B fully inside A: A 9:30-11:30, B 10:00-11:20
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(10, 0), _ts(11, 20)) is True


def test_intervals_overlap_partial_crossover():
    # A 9:30-11:30, B 11:00-12:30
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(11, 0), _ts(12, 30)) is True


def test_intervals_overlap_touching_not_overlapping():
    # A ends exactly when B starts: A 9:30-10:30, B 10:30-11:30
    # Strict inequality: NOT considered overlap.
    assert _intervals_overlap(_ts(9, 30), _ts(10, 30), _ts(10, 30), _ts(11, 30)) is False


def test_intervals_overlap_disjoint():
    # A 9:30-10:30, B 11:00-12:00
    assert _intervals_overlap(_ts(9, 30), _ts(10, 30), _ts(11, 0), _ts(12, 0)) is False


def test_intervals_overlap_same_start_different_end():
    # Today's classic same-start duplicate: A 9:30-11:30, B 9:30-11:00
    assert _intervals_overlap(_ts(9, 30), _ts(11, 30), _ts(9, 30), _ts(11, 0)) is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except AssertionError:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
    print(f"\n{passed}/{len(tests)} passed")
    sys.exit(0 if not failed else 1)
