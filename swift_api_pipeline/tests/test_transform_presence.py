# swift_api_pipeline/tests/test_transform_presence.py
"""missing_since on stg_organizations / stg_projects (migration 278, 2026-10-09).

The staging tables upsert and never delete, so an org the GC side removed from
Swift kept looking live. After every non-empty nightly upsert, transform.py
stamps missing_since = now() on rows the run did not return (once: the first
night) and clears it on rows that are back. The '-MANUAL_' placeholder orgs
were never in Swift and must never be marked.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from transform import MANUAL_ORG_PREFIX, mark_missing  # noqa: E402


class FakeDb:
    def __init__(self):
        self.calls = []

    def execute(self, query, *args, **kwargs):
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 0"


def test_orgs_mark_absent_rows_once_clear_returning_rows_and_skip_manual_orgs():
    db = FakeDb()
    mark_missing(db, "stg_organizations", "org_did", "run-1")
    assert len(db.calls) == 2
    mark, clear = db.calls
    assert mark[0] == ("UPDATE data_staging.stg_organizations SET missing_since = now() "
                       "WHERE run_id IS DISTINCT FROM $1 AND missing_since IS NULL "
                       "AND org_did NOT LIKE $2")
    assert mark[1] == ("run-1", MANUAL_ORG_PREFIX + "%")
    assert clear[0] == ("UPDATE data_staging.stg_organizations SET missing_since = NULL "
                        "WHERE run_id = $1 AND missing_since IS NOT NULL")
    assert clear[1] == ("run-1",)


def test_projects_have_no_manual_exclusion():
    db = FakeDb()
    mark_missing(db, "stg_projects", "project_did", "run-1")
    mark, clear = db.calls
    assert "NOT LIKE" not in mark[0] and mark[1] == ("run-1",)
    assert "stg_projects" in mark[0] and "stg_projects" in clear[0]


def test_first_night_only_the_mark_keeps_the_earliest_date():
    # "missing_since IS NULL" in the mark statement is what makes the stamp stick to
    # the first night a row went missing instead of moving every night.
    db = FakeDb()
    mark_missing(db, "stg_projects", "project_did", "run-2")
    assert "missing_since IS NULL" in db.calls[0][0]
