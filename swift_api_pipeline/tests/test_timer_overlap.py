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


from timer_correction_review import _build_overlap_clusters


def _entry(start_h, start_m, end_h, end_m):
    """Minimal entry dict with start_time and end_time."""
    return {
        "start_time": _ts(start_h, start_m),
        "end_time":   _ts(end_h,   end_m),
    }


def test_clusters_single_entry_yields_one_singleton():
    entries = [_entry(9, 30, 11, 30)]
    clusters = _build_overlap_clusters(entries)
    assert len(clusters) == 1
    assert len(clusters[0]) == 1


def test_clusters_two_overlapping_entries_merge():
    a = _entry(9, 30, 11, 30)
    b = _entry(10, 0, 11, 20)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_clusters_two_disjoint_entries_stay_separate():
    a = _entry(9, 30, 10, 30)
    b = _entry(11, 0, 12, 0)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 2


def test_clusters_three_way_transitive_merge():
    # A 9:00-10:30, B 10:00-11:00, C 10:30-12:00
    # A overlaps B, B overlaps C, A does NOT directly overlap C.
    # All three must end up in the same cluster.
    a = _entry(9, 0, 10, 30)
    b = _entry(10, 0, 11, 0)
    c = _entry(10, 30, 12, 0)
    clusters = _build_overlap_clusters([a, b, c])
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_clusters_same_start_different_end_merge():
    # Today's classic case still clusters.
    a = _entry(9, 30, 11, 30)
    b = _entry(9, 30, 11, 0)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_clusters_preserves_input_order_within_cluster():
    # Stability: the cluster lists entries in input order.
    a = _entry(9, 30, 11, 30)
    b = _entry(10, 0, 11, 20)
    clusters = _build_overlap_clusters([a, b])
    assert clusters[0][0] is a
    assert clusters[0][1] is b


def test_clusters_touching_endpoints_stay_separate():
    # A ends exactly when B starts -> not overlapping, two singletons.
    a = _entry(9, 30, 10, 30)
    b = _entry(10, 30, 11, 30)
    clusters = _build_overlap_clusters([a, b])
    assert len(clusters) == 2


from timer_correction_review import _make_group_id


def test_group_id_same_start_matches_legacy_formula():
    """Migration invariant: same-start clusters produce the same group_id
    under the new earliest-anchor rule as the legacy exact-match formula did.
    Walks the actual clustering pipeline (bucket -> _build_overlap_clusters
    -> earliest -> _make_group_id) instead of asserting f(x) == f(x).

    Failure of this test would mean existing pending Google Form threads
    get orphaned the next time detection runs.
    """
    project_did = "-OmzvGwfYsSskngv6SEo"
    user_email = "ryan@ontel.co"
    shared_start = _ts(12, 2)
    site_name = "SOUTHLAND HILLS TN - New Build "
    site_id = "Mid-South Communications/VZW/CGC/NSB/17455477/Apr 2026"
    task = "3. Live Review Complete 2"

    # Bucket of two entries sharing start_time but with different end_time --
    # the legacy "same-start duplicate" shape.
    bucket = [
        {"start_time": shared_start, "end_time": _ts(15, 36), "duration_min": 214},
        {"start_time": shared_start, "end_time": _ts(15, 30), "duration_min": 208},
    ]

    clusters = _build_overlap_clusters(bucket)
    assert len(clusters) == 1, f"expected one cluster, got {len(clusters)}"
    cluster = clusters[0]
    assert len(cluster) == 2, f"expected cluster size 2, got {len(cluster)}"

    # The earliest-anchor that the refactored detect_and_track_duplicates uses.
    earliest = min(e["start_time"] for e in cluster)
    # Sanity: clustering returned both same-start entries, so earliest must
    # equal the shared value -- this fact, not a math tautology, is what
    # carries the migration guarantee for same-start clusters.
    assert earliest == shared_start

    new_gid = _make_group_id(project_did, user_email, earliest,
                             site_name, site_id, task)
    legacy_gid = _make_group_id(project_did, user_email, shared_start,
                                site_name, site_id, task)

    assert new_gid == legacy_gid, (
        f"group_id changed for same-start cluster -- would orphan existing reviews. "
        f"new={new_gid} legacy={legacy_gid}"
    )


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
