# swift_api_pipeline/tests/test_inc_prune.py
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from extract_asset_tasks_inc import plan_asset_visits, plan_task_writes, epoch_to_ts

T1 = 1751990400000  # older epoch millis
T2 = 1752076800000  # newer


def _stored(did, ts):
    return {"asset_did": did, "last_updated": epoch_to_ts(ts)}


def test_epoch_to_ts():
    assert epoch_to_ts(None) is None
    dt = epoch_to_ts(T1)
    assert dt.tzinfo is not None and dt.year >= 2025


def test_new_asset_is_visited():
    visits, missing = plan_asset_visits([{"id": "a1", "lastUpdated": T1}], {})
    assert [a["id"] for a in visits] == ["a1"] and missing == set()


def test_unchanged_asset_is_skipped():
    visits, missing = plan_asset_visits(
        [{"id": "a1", "lastUpdated": T1}], {"a1": _stored("a1", T1)})
    assert visits == [] and missing == set()


def test_changed_asset_is_visited():
    visits, _ = plan_asset_visits(
        [{"id": "a1", "lastUpdated": T2}], {"a1": _stored("a1", T1)})
    assert [a["id"] for a in visits] == ["a1"]


def test_deleted_asset_is_reported_missing():
    visits, missing = plan_asset_visits([], {"a1": _stored("a1", T1)})
    assert visits == [] and missing == {"a1"}


def test_task_writes_only_changed():
    fetched = [
        {"id": "t1", "collection": "asset-tasks", "lastUpdated": T1},
        {"id": "t2", "collection": "asset-tasks", "lastUpdated": T2},
    ]
    stored = {"t1": epoch_to_ts(T1), "t2": epoch_to_ts(T1)}
    writes, missing = plan_task_writes(fetched, stored)
    assert [t["id"] for t in writes] == ["t2"] and missing == set()


def test_task_deletion_detected():
    writes, missing = plan_task_writes([], {"t1": epoch_to_ts(T1)})
    assert writes == [] and missing == {"t1"}


def test_missing_last_updated_always_written():
    """The anomalous live row (Task 1 probe): no lastUpdated, no metrics at
    all. Fail-open rule: treat as always changed, never staleness."""
    fetched = [{"id": "t1", "collection": "asset-tasks"}]
    stored = {"t1": epoch_to_ts(T1)}
    writes, missing = plan_task_writes(fetched, stored)
    assert [t["id"] for t in writes] == ["t1"] and missing == set()


def test_milestone_rows_are_ignored_entirely():
    """Milestone rows repeat under every asset and are the only id
    duplicates in the raw asset-tasks listing. plan_task_writes must
    consider ONLY collection == "asset-tasks" rows: milestone rows are
    not written, and not counted as fetched for deletion detection."""
    fetched = [
        {"id": "m1", "collection": "milestones", "lastUpdated": T2},
        {"id": "t1", "collection": "asset-tasks", "lastUpdated": T1},
    ]
    stored = {"t1": epoch_to_ts(T1)}
    writes, missing = plan_task_writes(fetched, stored)
    # Same result as if the milestone row were absent from the fetch:
    # t1 unchanged -> no write, and m1 is never a deletion candidate
    # because it was never stored (and never eligible to be stored).
    assert writes == [] and missing == set()
