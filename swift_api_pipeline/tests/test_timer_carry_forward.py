"""Timer transform: (project, member) pairs that vanish from Swift's report are carried forward.

Background (2026-09-15, Cho Jebulan Sep 1-4): Swift's timer report stops returning a
member the moment the account is deactivated. transform_timer_activities replaces the
whole month bucket (DELETE WHERE start_date + INSERT of the newest run), so the first
nightly run after deactivation erased the member's month-to-date from staging, clean
and every export. Raw is append-only, so the rows survive there; the transform must
copy them back from the last run that still carried the member. Keyed per project so
a run where one project's extraction failed keeps that project's rows for everyone.

DB is faked with the house duck-typed stub (no unittest.mock, no network).
"""
import logging
import os
import sys
import uuid
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transform  # noqa: E402
from transform import (  # noqa: E402
    VANISHED_MEMBERS_SQL,
    carry_forward_vanished_members,
    timer_raw_record_to_row,
    transform_timer_activities,
)

RUN_NEW = uuid.UUID("11111111-1111-1111-1111-111111111111")
RUN_OLD = uuid.UUID("22222222-2222-2222-2222-222222222222")
BUCKET = date(2026, 9, 1)
TS19 = "-OmzvGwfYsSskngv6SEo"
TS20 = "-Ozh7xx_YHk8yXEmVmFR"
CHO = "cho.jebulan@ontel.co"


def _raw(run_id, run_date, email, did=TS19, project="TECH-OPS: TS19", minutes=60,
         start="2026-09-03T09:00:22-04:00", end="2026-09-03T10:00:22-04:00"):
    return {
        "id": 1, "loaded_at": datetime(2026, 9, 7, 5, 18), "run_id": run_id, "run_date": run_date,
        "start_date": BUCKET, "end_date": date(2026, 9, 6), "project_did": did,
        "data": {
            "Project": project, "Site Name": "Site A", "Site ID": "S1", "Task": "3. Closeout 2",
            "Start Time": start, "End Time": end, "Duration (min)": minutes,
            "User Name": email.split("@")[0].title(), "User Email": email, "User Role": "TA",
        },
    }


def _vanished(email, did=TS19, project="TECH-OPS: TS19", run_date=date(2026, 9, 7), n=1, absent=False):
    return {"project_did": did, "project": project, "email": email, "run_id": RUN_OLD,
            "run_date": run_date, "n": n, "project_absent": absent}


class FakeDb:
    """Routes each SQL to a scripted answer by a distinctive substring."""

    def __init__(self, current_rows, vanished, source_rows, meta=True):
        self.current_rows = current_rows
        self.vanished = vanished            # rows of VANISHED_MEMBERS_SQL
        self.source_rows = source_rows      # {(run_id, did, email): [raw records]}
        self.meta = meta
        self.executed = []
        self.inserted = []                  # one list per executemany call

    def fetchrow(self, sql, *args):
        assert "SELECT run_date, start_date, end_date" in sql
        if not self.meta:
            return None
        return {"run_date": date(2026, 9, 8), "start_date": BUCKET, "end_date": date(2026, 9, 7)}

    def fetch(self, sql, *args):
        if sql.strip() == VANISHED_MEMBERS_SQL.strip():
            assert (str(args[0]), args[1]) == (str(RUN_NEW), BUCKET)
            return self.vanished
        if "WHERE run_id = $1 AND project_did = $2 AND data->>'User Email' = $3" in sql:
            return self.source_rows.get((args[0], args[1], args[2]), [])
        if "WHERE run_id = $1" in sql:
            assert str(args[0]) == str(RUN_NEW)   # production passes run_id as str
            return self.current_rows
        raise AssertionError(f"unexpected fetch: {sql[:80]}")

    def fetchval(self, sql, *args):
        return 0

    def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))

    def executemany(self, sql, rows):
        assert "INSERT INTO" in sql and "stg_timer_activities" in sql
        self.inserted.append(list(rows))


def _warnings(caplog):
    return " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_row_mapping_keeps_the_record_own_run_provenance():
    rec = _raw(RUN_OLD, date(2026, 9, 7), CHO)
    row = timer_raw_record_to_row(rec)
    assert row[0] == "TECH-OPS: TS19" and row[1] == 19 and row[2] == TS19
    assert row[5] == "3. Closeout 2" and row[6] == "Closeout"
    assert row[15] == 60 and row[17] == CHO
    # provenance comes from the RECORD, not from the run being transformed
    assert row[19:23] == (RUN_OLD, date(2026, 9, 7), BUCKET, date(2026, 9, 6))


def test_vanished_member_is_reinserted_from_her_last_run_per_project(caplog):
    current = [_raw(RUN_NEW, date(2026, 9, 8), "still.here@ontel.co")]
    src = {(RUN_OLD, TS19, CHO): [_raw(RUN_OLD, date(2026, 9, 7), CHO, minutes=30)],
           (RUN_OLD, TS20, CHO): [_raw(RUN_OLD, date(2026, 9, 7), CHO, did=TS20,
                                       project="TECH-OPS: TS20", minutes=45)]}
    db = FakeDb(current, [_vanished(CHO), _vanished(CHO, did=TS20, project="TECH-OPS: TS20")], src)
    with caplog.at_level(logging.WARNING, logger=transform.logger.name):
        total, carried = transform_timer_activities(db, str(RUN_NEW))

    assert total == 1                                   # the run's own rows only
    assert len(db.inserted) == 3                        # main insert + one per (project, member)
    rows = db.inserted[1] + db.inserted[2]
    assert [r[17] for r in rows] == [CHO] * 2
    assert {r[19] for r in rows} == {RUN_OLD}           # source run kept for provenance
    assert sorted(r[15] for r in rows) == [30, 45]
    assert [(c["email"], c["rows"], c["project_absent"]) for c in carried] == [(CHO, 1, False), (CHO, 1, False)]
    msg = _warnings(caplog)
    assert CHO in msg and "2 rows" in msg and "2026-09-07" in msg and "deactivated" in msg
    assert "extraction failure" not in msg


def test_no_vanished_pair_means_no_extra_insert_and_no_warning(caplog):
    db = FakeDb([_raw(RUN_NEW, date(2026, 9, 8), "still.here@ontel.co")], [], {})
    with caplog.at_level(logging.WARNING, logger=transform.logger.name):
        total, carried = transform_timer_activities(db, str(RUN_NEW))
    assert len(db.inserted) == 1 and carried == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_run_with_no_raw_rows_returns_before_deleting_the_bucket():
    """meta comes from the run's own raw rows, so a rowless run never touches staging."""
    db = FakeDb([], [_vanished(CHO)], {}, meta=False)
    assert transform_timer_activities(db, str(RUN_NEW)) == 0
    assert db.executed == [] and db.inserted == []


def test_failed_project_is_carried_forward_for_a_member_still_present_elsewhere(caplog):
    """Partial extraction: TS19 returned nothing; a member with TS20 rows in this run
    still gets her TS19 rows back, and the project outage is flagged."""
    current = [_raw(RUN_NEW, date(2026, 9, 8), "busy@ontel.co", did=TS20, project="TECH-OPS: TS20")]
    src = {(RUN_OLD, TS19, "busy@ontel.co"): [_raw(RUN_OLD, date(2026, 9, 7), "busy@ontel.co")]}
    db = FakeDb(current, [_vanished("busy@ontel.co", absent=True)], src)
    with caplog.at_level(logging.WARNING, logger=transform.logger.name):
        total, carried = transform_timer_activities(db, str(RUN_NEW))
    assert total == 1 and len(db.inserted) == 2
    assert db.inserted[1][0][2] == TS19 and db.inserted[1][0][17] == "busy@ontel.co"
    assert carried[0]["project_absent"] is True
    msg = _warnings(caplog)
    assert "TECH-OPS: TS19" in msg and "extraction failure" in msg
    assert "deactivated" not in msg                     # not presented as a resignation


def test_carry_forward_returns_per_pair_summary():
    db = FakeDb([], [_vanished("a@ontel.co"), _vanished("b@ontel.co", run_date=date(2026, 9, 6))],
                {(RUN_OLD, TS19, "a@ontel.co"): [_raw(RUN_OLD, date(2026, 9, 7), "a@ontel.co")],
                 (RUN_OLD, TS19, "b@ontel.co"): [_raw(RUN_OLD, date(2026, 9, 6), "b@ontel.co")]})
    carried = carry_forward_vanished_members(db, RUN_NEW, BUCKET)
    assert [(c["project_did"], c["email"], c["rows"]) for c in carried] == [
        (TS19, "a@ontel.co", 1), (TS19, "b@ontel.co", 1)]


def test_vanished_sql_only_looks_at_runs_loaded_before_this_one():
    """Replaying an old run must not pull rows from runs loaded after it."""
    assert "loaded_at < (SELECT MIN(loaded_at)" in VANISHED_MEMBERS_SQL
    assert "DISTINCT ON (p.project_did, p.email)" in VANISHED_MEMBERS_SQL
