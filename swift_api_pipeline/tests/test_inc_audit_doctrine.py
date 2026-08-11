"""Doctrine rule 4 (lifecycle lag) tests for audit_asset_tasks_inc.

The 2026-08-06 root cause left one residual diff class: task-only edits
(assign/schedule) never bump the asset-project lastUpdated, so the walker
misses them until the weekly full-walk sweep; conversely the shadow walks
hourly while stg_asset_tasks reloads nightly, so the shadow legitimately
leads CURRENT on submit/approve for up to a day. Rule 4 (approved by
Jamil 2026-08-10) tolerates exactly that bounded lag — and nothing else:

  - only lifecycle columns (status, assignment/schedule, submit/approve/
    cancel groups) may differ;
  - assignment/schedule may lag in either direction (blind spot vs
    nightly lag);
  - status and event columns must be AHEAD on the INC side only — the
    walker seeing an asset-touching event LATE is a real bug, not lag;
  - differing event dates on INC must be recent (LAG_CAP_DAYS);
  - the tolerance is only active while the weekly sweep is alive
    (last successful baseline/full-walk within SWEEP_FRESH_DAYS).
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from audit_asset_tasks_inc import (
    COMPARE_COLS,
    LAG_CAP_DAYS,
    SWEEP_FRESH_DAYS,
    _classify_column_diffs,
    _is_lifecycle_lag,
    _lag_tolerance_active,
)

TODAY = date(2026, 8, 10)


def make_row(**overrides):
    """A shared-row record as _classify_column_diffs fetches it: identical
    on both sides except for the given c_/i_ overrides."""
    row = {"task_did": "T1"}
    for col in COMPARE_COLS:
        row[f"c_{col}"] = None
        row[f"i_{col}"] = None
    row.update(
        c_task_status="pending", i_task_status="pending",
        c_task_name="Install", i_task_name="Install",
    )
    row.update(overrides)
    return row


def diff_cols(row):
    return [c for c in COMPARE_COLS if row[f"c_{c}"] != row[f"i_{c}"]]


def lag(row, today=TODAY):
    return _is_lifecycle_lag(diff_cols(row), row, today)


# ---- tolerated shapes -------------------------------------------------


def test_inc_ahead_approval_package_is_lifecycle_lag():
    """The dominant observed shape: shadow already has today's approval,
    nightly CURRENT still pending."""
    row = make_row(
        c_task_status="pending", i_task_status="approved",
        i_task_submitted_on=TODAY, i_task_approved_on=TODAY,
        i_task_submitted_by_did="U1", i_task_submitted_by_name="Kyla Palo",
        i_task_submitted_by_email="kyla.palo@ontel.co",
        i_task_approved_by_did="U1", i_task_approved_by_name="Kyla Palo",
        i_task_approved_by_email="kyla.palo@ontel.co",
    )
    assert lag(row)


def test_inc_ahead_submit_only_package_is_lifecycle_lag():
    row = make_row(
        c_task_status="pending", i_task_status="submitted",
        i_task_submitted_on=TODAY - timedelta(days=1),
        i_task_submitted_by_did="U1", i_task_submitted_by_name="Kyla Palo",
        i_task_submitted_by_email="kyla.palo@ontel.co",
    )
    assert lag(row)


def test_assignment_lag_inc_trailing_is_lifecycle_lag():
    """The walker blind spot: assignment bumps only the task lastUpdated,
    INC stays null until the weekly sweep."""
    row = make_row(
        c_task_scheduled=TODAY, i_task_scheduled=None,
        c_task_assigned_to_did="TEAM1", i_task_assigned_to_did=None,
        c_task_assigned_to_name="VZ2 - CG1", i_task_assigned_to_name=None,
        c_task_assigned_to_collection="teams", i_task_assigned_to_collection=None,
    )
    assert lag(row)


def test_assignment_lag_inc_ahead_is_lifecycle_lag():
    row = make_row(
        c_task_scheduled=None, i_task_scheduled=date(2026, 8, 11),
        c_task_assigned_to_did=None, i_task_assigned_to_did="TEAM1",
        c_task_assigned_to_name=None, i_task_assigned_to_name="VZ2 - CG1",
        c_task_assigned_to_collection=None, i_task_assigned_to_collection="teams",
    )
    assert lag(row)


def test_reassignment_both_sides_non_null_is_lifecycle_lag():
    row = make_row(
        c_task_assigned_to_did="TEAM1", i_task_assigned_to_did="TEAM2",
        c_task_assigned_to_name="VZ2 - CG1", i_task_assigned_to_name="VZ2 - CG2",
    )
    assert lag(row)


# ---- NOT tolerated ----------------------------------------------------


def test_inc_trailing_on_status_is_not_lag():
    """Submit/approve touch the asset, so the walker must see them within
    one walk: INC behind on status is a bug signal, never lag."""
    row = make_row(
        c_task_status="approved", i_task_status="pending",
        c_task_approved_on=TODAY, i_task_approved_on=None,
    )
    assert not lag(row)


def test_inc_trailing_on_event_column_is_not_lag():
    row = make_row(
        c_task_submitted_on=TODAY, i_task_submitted_on=None,
        c_task_submitted_by_did="U1", i_task_submitted_by_did=None,
    )
    assert not lag(row)


def test_stale_event_date_is_not_lag():
    row = make_row(
        c_task_status="pending", i_task_status="submitted",
        i_task_submitted_on=TODAY - timedelta(days=LAG_CAP_DAYS + 1),
        i_task_submitted_by_did="U1",
    )
    assert not lag(row)


def test_event_date_exactly_at_cap_is_lag():
    row = make_row(
        c_task_status="pending", i_task_status="submitted",
        i_task_submitted_on=TODAY - timedelta(days=LAG_CAP_DAYS),
        i_task_submitted_by_did="U1",
    )
    assert lag(row)


def test_non_lifecycle_column_in_mix_is_not_lag():
    row = make_row(
        c_task_status="pending", i_task_status="submitted",
        i_task_submitted_on=TODAY,
        c_task_name="Install", i_task_name="Install v2",
    )
    assert not lag(row)


def test_unknown_status_value_is_not_lag():
    row = make_row(c_task_status="pending", i_task_status="wtf_new_status")
    assert not lag(row)


def test_datetime_event_value_is_normalized():
    row = make_row(
        c_task_status="pending", i_task_status="submitted",
        i_task_submitted_on=datetime(2026, 8, 9, 14, 30),
        i_task_submitted_by_did="U1",
    )
    assert lag(row)


# ---- sweep-freshness gate --------------------------------------------


def test_tolerance_active_with_fresh_sweep():
    now = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    assert _lag_tolerance_active(now - timedelta(days=2), now)


def test_tolerance_inactive_when_sweep_stale():
    now = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    stale = now - timedelta(days=SWEEP_FRESH_DAYS, hours=1)
    assert not _lag_tolerance_active(stale, now)


def test_tolerance_inactive_when_no_sweep_ever():
    now = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    assert not _lag_tolerance_active(None, now)


def test_tolerance_active_at_exact_freshness_boundary():
    now = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    assert _lag_tolerance_active(now - timedelta(days=SWEEP_FRESH_DAYS), now)


# ---- classification wiring -------------------------------------------


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self, sql, *args, **kwargs):
        return self.rows


def _lag_row(task_did):
    row = make_row(
        c_task_status="pending", i_task_status="approved",
        i_task_approved_on=TODAY, i_task_approved_by_did="U1",
        i_task_approved_by_name="Kyla Palo",
        i_task_approved_by_email="kyla.palo@ontel.co",
    )
    row["task_did"] = task_did
    return row


def _dangling_row(task_did):
    row = make_row(
        c_task_approved_by_did="GHOST", i_task_approved_by_did=None,
    )
    row["task_did"] = task_did
    return row


def _unexplained_row(task_did):
    row = make_row(c_task_name="Install", i_task_name="Install v2")
    row["task_did"] = task_did
    return row


def test_classify_counts_all_three_categories():
    db = FakeDb([_dangling_row("A"), _lag_row("B"), _unexplained_row("C")])
    dangling, lifecycle_lag, unexplained, sample = _classify_column_diffs(
        db, "P1", today=TODAY, lag_active=True)
    assert (dangling, lifecycle_lag, unexplained) == (1, 1, 1)
    assert list(sample) == ["C"]


def test_classify_with_tolerance_off_counts_lag_as_unexplained():
    db = FakeDb([_lag_row("B")])
    dangling, lifecycle_lag, unexplained, sample = _classify_column_diffs(
        db, "P1", today=TODAY, lag_active=False)
    assert (dangling, lifecycle_lag, unexplained) == (0, 0, 1)
    assert list(sample) == ["B"]


def test_classify_defaults_to_strict_without_explicit_lag_active():
    """A caller that doesn't thread the sweep-freshness gate must get the
    strict pre-rule-4 behavior, never silent tolerance."""
    db = FakeDb([_lag_row("B")])
    dangling, lifecycle_lag, unexplained, _ = _classify_column_diffs(
        db, "P1", today=TODAY)
    assert (dangling, lifecycle_lag, unexplained) == (0, 0, 1)


def test_dangling_classification_takes_precedence_over_lag():
    """A did-only diff with names NULL on both sides is rule 3 (dangling),
    even though *_by_did is also a lifecycle column."""
    db = FakeDb([_dangling_row("A")])
    dangling, lifecycle_lag, unexplained, _ = _classify_column_diffs(
        db, "P1", today=TODAY, lag_active=True)
    assert (dangling, lifecycle_lag, unexplained) == (1, 0, 0)
