"""Timer transform: members who vanish from Swift's report mid-month are carried forward.

Background (2026-09-15, Cho Jebulan Sep 1-4): Swift's timer report stops returning a
member the moment the account is deactivated. transform_timer_activities replaces the
whole month bucket (DELETE WHERE start_date + INSERT of the newest run), so the first
nightly run after deactivation erased the member's month-to-date from staging, clean
and every export. Raw is append-only, so the rows survive there; the transform must
copy them back from the last run that still carried the member.

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


def _raw(run_id, run_date, email, start="2026-09-03T09:00:22-04:00", end="2026-09-03T10:00:22-04:00",
         minutes=60, project="TECH-OPS: TS19"):
    return {
        "id": 1, "loaded_at": datetime(2026, 9, 7, 5, 18), "run_id": run_id, "run_date": run_date,
        "start_date": BUCKET, "end_date": date(2026, 9, 6), "project_did": "-OmzvGwfYsSskngv6SEo",
        "data": {
            "Project": project, "Site Name": "Site A", "Site ID": "S1", "Task": "3. Closeout 2",
            "Start Time": start, "End Time": end, "Duration (min)": minutes,
            "User Name": email.split("@")[0].title(), "User Email": email, "User Role": "TA",
        },
    }


class FakeDb:
    """Routes each SQL to a scripted answer by a distinctive substring."""

    def __init__(self, current_rows, vanished, source_rows):
        self.current_rows = current_rows
        self.vanished = vanished            # rows of VANISHED_MEMBERS_SQL
        self.source_rows = source_rows      # {(run_id, email): [raw records]}
        self.executed = []
        self.inserted = []                  # one list per executemany call

    def fetchrow(self, sql, *args):
        assert "SELECT run_date, start_date, end_date" in sql
        return {"run_date": date(2026, 9, 8), "start_date": BUCKET, "end_date": date(2026, 9, 7)}

    def fetch(self, sql, *args):
        if sql.strip() == VANISHED_MEMBERS_SQL.strip():
            assert (str(args[0]), args[1]) == (str(RUN_NEW), BUCKET)
            return self.vanished
        if "WHERE run_id = $1 AND data->>'User Email' = $2" in sql:
            return self.source_rows.get((args[0], args[1]), [])
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


def test_row_mapping_keeps_the_record_own_run_provenance():
    rec = _raw(RUN_OLD, date(2026, 9, 7), "cho.jebulan@ontel.co")
    row = timer_raw_record_to_row(rec)
    assert row[0] == "TECH-OPS: TS19" and row[1] == 19 and row[2] == "-OmzvGwfYsSskngv6SEo"
    assert row[5] == "3. Closeout 2" and row[6] == "Closeout"
    assert row[15] == 60 and row[17] == "cho.jebulan@ontel.co"
    # provenance comes from the RECORD, not from the run being transformed
    assert row[19:23] == (RUN_OLD, date(2026, 9, 7), BUCKET, date(2026, 9, 6))


def test_vanished_member_is_reinserted_from_her_last_run(caplog):
    current = [_raw(RUN_NEW, date(2026, 9, 8), "still.here@ontel.co")]
    cho_rows = [_raw(RUN_OLD, date(2026, 9, 7), "cho.jebulan@ontel.co", minutes=30),
                _raw(RUN_OLD, date(2026, 9, 7), "cho.jebulan@ontel.co", minutes=45)]
    db = FakeDb(current, [{"email": "cho.jebulan@ontel.co", "run_id": RUN_OLD,
                           "run_date": date(2026, 9, 7), "n": 2}],
                {(RUN_OLD, "cho.jebulan@ontel.co"): cho_rows})
    with caplog.at_level(logging.WARNING, logger=transform.logger.name):
        total = transform_timer_activities(db, str(RUN_NEW))

    assert total == 1                                   # the run's own rows only
    assert len(db.inserted) == 2                        # main insert + carry-forward insert
    carried = db.inserted[1]
    assert [r[17] for r in carried] == ["cho.jebulan@ontel.co"] * 2
    assert {r[19] for r in carried} == {RUN_OLD}        # source run kept for provenance
    assert sorted(r[15] for r in carried) == [30, 45]
    msg = " ".join(rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING)
    assert "cho.jebulan@ontel.co" in msg and "2 rows" in msg and "2026-09-07" in msg


def test_no_vanished_member_means_no_extra_insert_and_no_warning(caplog):
    db = FakeDb([_raw(RUN_NEW, date(2026, 9, 8), "still.here@ontel.co")], [], {})
    with caplog.at_level(logging.WARNING, logger=transform.logger.name):
        transform_timer_activities(db, str(RUN_NEW))
    assert len(db.inserted) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_empty_run_still_carries_forward_instead_of_leaving_the_bucket_empty():
    """A run that returned nothing used to DELETE the bucket and return early."""
    lou = [_raw(RUN_OLD, date(2026, 9, 7), "lourvina.olimpo@ontel.co")]
    db = FakeDb([], [{"email": "lourvina.olimpo@ontel.co", "run_id": RUN_OLD,
                      "run_date": date(2026, 9, 7), "n": 1}],
                {(RUN_OLD, "lourvina.olimpo@ontel.co"): lou})
    total = transform_timer_activities(db, str(RUN_NEW))
    assert total == 0
    assert len(db.inserted) == 1 and db.inserted[0][0][17] == "lourvina.olimpo@ontel.co"


def test_carry_forward_returns_per_member_summary():
    db = FakeDb([], [{"email": "a@ontel.co", "run_id": RUN_OLD, "run_date": date(2026, 9, 7), "n": 1},
                     {"email": "b@ontel.co", "run_id": RUN_OLD, "run_date": date(2026, 9, 6), "n": 1}],
                {(RUN_OLD, "a@ontel.co"): [_raw(RUN_OLD, date(2026, 9, 7), "a@ontel.co")],
                 (RUN_OLD, "b@ontel.co"): [_raw(RUN_OLD, date(2026, 9, 6), "b@ontel.co")]})
    carried = carry_forward_vanished_members(db, RUN_NEW, BUCKET)
    assert [(c["email"], c["rows"]) for c in carried] == [("a@ontel.co", 1), ("b@ontel.co", 1)]
