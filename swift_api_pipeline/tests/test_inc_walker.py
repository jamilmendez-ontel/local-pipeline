# swift_api_pipeline/tests/test_inc_walker.py
"""Unit tests for task_to_stg_row: column order + audit-hash column
population, using a synthetic payload dict. No live API/DB calls."""
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from extract_asset_tasks_inc import task_to_stg_row, STG_COLS

STG_COL_LIST = [c.strip() for c in STG_COLS.split(",")]

PROJECT = {"project_did": "proj-1", "status": "active", "lastUpdated": 1752076800000}

# Asset-project row shape per the 2026-07-10 audit findings: the export the
# current pipeline mirrors uses the ASSET-PROJECT's own status (as
# "Project_Status"), the underlying asset.id (as Asset_DID), and the bare
# shortName (as Asset_Name). "id" is the asset-project id (walk-scope key
# only, not a stg column); "name" is project-qualified and unused in stg.
ASSET = {
    "id": "asset-core-1proj-1",
    "asset": {"id": "asset-core-1", "name": "John Smith"},
    "identifier": "EMP-042",
    "name": "TECH-OPS: TS99 | John Smith",
    "shortName": "John Smith",
    "status": "pending",
    "metrics": {"reqCount": 5},
    "lastUpdated": 1752076800000,
}

TASK = {
    "id": "task-1",
    "collection": "asset-tasks",
    "name": "1. Install Equipment 2",
    "status": "approved",
    "scheduled": 1752076800000,
    "lastUpdated": 1752076900000,
    "assignedTo": {
        "id": "person-assigned", "collection": "personnel",
        "name": "Assignee Name",
    },
    "submittedBy": {
        "id": "person-sub", "collection": "personnel",
        "name": "Submitter Name", "email": "submitter@example.com",
    },
    "submittedOn": 1752000000000,
    "approvedBy": {
        "id": "person-app", "collection": "personnel",
        "name": "Approver Name", "email": "approver@example.com",
    },
    "approvedOn": 1752050000000,
    "cancelledBy": None,
    "cancelledOn": None,
}


def test_column_order_matches_stg_cols():
    row = task_to_stg_row(PROJECT, ASSET, TASK)
    assert len(row) == len(STG_COL_LIST)


def test_field_values_land_in_the_right_position():
    row = task_to_stg_row(PROJECT, ASSET, TASK)
    by_col = dict(zip(STG_COL_LIST, row))

    assert by_col["task_did"] == "task-1"
    assert by_col["project_did"] == "proj-1"
    # project_status carries the ASSET-PROJECT's status (export semantics),
    # not the project row's status ("active" above must NOT leak through).
    assert by_col["project_status"] == "pending"
    assert by_col["asset_did"] == "asset-core-1"
    assert by_col["asset_id"] == "EMP-042"
    assert by_col["asset_name"] == "John Smith"
    assert by_col["asset_requirement_count"] == 5

    assert by_col["task_assigned_to_did"] == "person-assigned"
    assert by_col["task_assigned_to_collection"] == "personnel"
    assert by_col["task_assigned_to_name"] == "Assignee Name"
    assert by_col["task_assigned_to_email"] is None  # no 'email' key on this dict

    assert by_col["task_submitted_by_did"] == "person-sub"
    assert by_col["task_submitted_by_name"] == "Submitter Name"
    assert by_col["task_submitted_by_email"] == "submitter@example.com"

    assert by_col["task_approved_by_did"] == "person-app"
    assert by_col["task_approved_by_email"] == "approver@example.com"

    assert by_col["task_cancelled_by_did"] is None
    assert by_col["task_cancelled_on"] is None


def test_audit_hash_columns_are_populated():
    """task_did, task_status, task_scheduled, task_name_clean must always be
    sourced correctly: these are the columns the drift audit hashes."""
    row = task_to_stg_row(PROJECT, ASSET, TASK)
    by_col = dict(zip(STG_COL_LIST, row))

    assert by_col["task_did"] == "task-1"
    assert by_col["task_status"] == "approved"
    assert by_col["task_scheduled"] == date(2025, 7, 9)
    assert by_col["task_name_clean"] == "Install Equipment"


def test_task_name_clean_strips_prefix_and_suffix():
    row = task_to_stg_row(PROJECT, ASSET, TASK)
    by_col = dict(zip(STG_COL_LIST, row))
    assert by_col["task_name"] == "1. Install Equipment 2"
    assert by_col["task_name_clean"] == "Install Equipment"


def test_last_updated_is_a_datetime():
    row = task_to_stg_row(PROJECT, ASSET, TASK)
    by_col = dict(zip(STG_COL_LIST, row))
    assert by_col["last_updated"] is not None
    assert by_col["last_updated"].tzinfo is not None
