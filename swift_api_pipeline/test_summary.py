"""Smoke tests for the Daily Task Summary helpers.

Usage:
    cd swift_api_pipeline
    venv/Scripts/python test_summary.py

No external deps — matches pytest/unittest test-file naming convention
so this file is committed and runnable by anyone on the team.
"""
from datetime import datetime, timezone, timedelta

from timer_correction_review import _compute_summary_groups


def _entry(task_clean, site_name, project, project_did, site_id,
           start_time, duration_min, user_email="test@ontel.co"):
    """Build a timer-entry-shaped dict for fixtures."""
    return {
        "project": project,
        "project_did": project_did,
        "user_email": user_email,
        "start_time": start_time,
        "end_time": start_time + timedelta(minutes=duration_min),
        "duration_min": duration_min,
        "site_name": site_name,
        "site_id": site_id,
        "task": task_clean,          # raw == clean for fixture simplicity
        "task_clean": task_clean,
    }


def test_groups_single_entry():
    """One entry -> one group, no duplicate flag."""
    t = datetime(2026, 4, 14, 13, 0, tzinfo=timezone.utc)
    entries = [_entry("COP Review", "SITE_A", "ProjX", "did_1", "sid_1", t, 60)]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1, f"expected 1 group, got {len(groups)}"
    g = groups[0]
    assert g["task"] == "COP Review"
    assert g["site"] == "SITE_A"
    assert g["project"] == "ProjX"
    assert g["entries"] == 1
    assert g["total_duration_min"] == 60
    assert g["has_duplicates"] is False
    print("PASS test_groups_single_entry")


def test_groups_same_task_different_sites_stay_split():
    """Same task at two sites -> two separate groups."""
    t1 = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t1, 60),
        _entry("COP Review", "SITE_B", "ProjX", "did_1", "sidB", t2, 60),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 2
    sites = sorted(g["site"] for g in groups)
    assert sites == ["SITE_A", "SITE_B"]
    print("PASS test_groups_same_task_different_sites_stay_split")


def test_groups_sum_durations_same_task_site_project():
    """Multiple entries, same task+site+project, different start_times -> one group, summed."""
    t1 = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t1, 60),
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t2, 45),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["entries"] == 2
    assert groups[0]["total_duration_min"] == 105
    # Different start_times -> not a duplicate
    assert groups[0]["has_duplicates"] is False
    print("PASS test_groups_sum_durations_same_task_site_project")


def test_groups_sort_by_duration_desc():
    """Groups should be ordered by total duration descending."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        _entry("Short Task", "SITE_A", "ProjX", "did_1", "sidA", t, 15),
        _entry("Long Task",  "SITE_A", "ProjX", "did_1", "sidA", t, 120),
        _entry("Mid Task",   "SITE_A", "ProjX", "did_1", "sidA", t, 60),
    ]

    groups = _compute_summary_groups(entries)

    tasks = [g["task"] for g in groups]
    assert tasks == ["Long Task", "Mid Task", "Short Task"], f"wrong order: {tasks}"
    print("PASS test_groups_sort_by_duration_desc")


def test_groups_flags_duplicates():
    """Two entries sharing the full duplicate key -> has_duplicates=True."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    # Same everything except end_time/duration (classic Swift sync duplicate)
    entries = [
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 60),
        _entry("COP Review", "SITE_A", "ProjX", "did_1", "sidA", t, 75),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["entries"] == 2
    assert groups[0]["total_duration_min"] == 135
    assert groups[0]["has_duplicates"] is True, "should flag duplicate"
    print("PASS test_groups_flags_duplicates")


def test_groups_handles_null_project():
    """None project groups as empty string and still produces a row."""
    t = datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc)
    entries = [
        # project is None — real Swift data sometimes has this
        _entry("Admin Overhead", "SITE_A", None, "did_1", "sidA", t, 30),
    ]

    groups = _compute_summary_groups(entries)

    assert len(groups) == 1
    assert groups[0]["project"] == ""
    assert groups[0]["task"] == "Admin Overhead"
    assert groups[0]["total_duration_min"] == 30
    print("PASS test_groups_handles_null_project")


if __name__ == "__main__":
    test_groups_single_entry()
    test_groups_same_task_different_sites_stay_split()
    test_groups_sum_durations_same_task_site_project()
    test_groups_sort_by_duration_desc()
    test_groups_flags_duplicates()
    test_groups_handles_null_project()
    print("\nAll tests passed.")
