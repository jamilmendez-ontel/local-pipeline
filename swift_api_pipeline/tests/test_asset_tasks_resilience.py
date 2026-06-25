# swift_api_pipeline/tests/test_asset_tasks_resilience.py
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline_notifier import PipelineOutcome
from extract_asset_tasks import detect_abnormal_counts, ABNORMAL_DROP_PCT, MAX_RETRY_ROUNDS
from base_extractor import BaseExtractor


def test_outcome_clean_when_no_problems():
    o = PipelineOutcome(run_id="r1")
    assert o.is_clean() is True
    assert o.email_status() == "SUCCESS"


def test_outcome_partial_takes_precedence_over_abnormal():
    o = PipelineOutcome(run_id="r1", failed_projects=["TS19"], abnormal_projects=["TS13"])
    assert o.is_clean() is False
    assert o.email_status() == "PARTIAL FAILURE"


def test_outcome_abnormal_when_only_counts_off():
    o = PipelineOutcome(run_id="r1", abnormal_projects=["TS13"])
    assert o.is_clean() is False
    assert o.email_status() == "ABNORMAL ROW COUNT"


def test_zero_rows_is_abnormal_even_with_no_baseline():
    assert detect_abnormal_counts({"TS13": 0}, {}) == ["TS13"]


def test_no_baseline_nonzero_is_skipped():
    assert detect_abnormal_counts({"TS13": 100}, {}) == []


def test_drop_beyond_tolerance_is_abnormal():
    # baseline 1000, current 850 -> 15% drop > 10%
    assert detect_abnormal_counts({"TS13": 850}, {"TS13": 1000}) == ["TS13"]


def test_small_dip_within_tolerance_is_clean():
    # baseline 1000, current 950 -> 5% drop <= 10%
    assert detect_abnormal_counts({"TS13": 950}, {"TS13": 1000}) == []


def test_exact_threshold_boundary_is_clean():
    # baseline 1000, current 900 == exactly 10% drop; strict "<" means NOT abnormal
    assert detect_abnormal_counts({"TS13": 900}, {"TS13": 1000}) == []


def test_growth_is_clean():
    assert detect_abnormal_counts({"TS13": 1200}, {"TS13": 1000}) == []


def test_multiple_projects_sorted():
    cur = {"TS19": 0, "TS13": 100, "TS16": 50}
    base = {"TS19": 500, "TS13": 100, "TS16": 1000}
    # TS19 zero -> abnormal; TS16 50 vs 1000 -> abnormal; TS13 flat -> clean
    assert detect_abnormal_counts(cur, base) == ["TS16", "TS19"]


def test_constants():
    assert MAX_RETRY_ROUNDS == 3
    assert ABNORMAL_DROP_PCT == 0.10


# ---------------------------------------------------------------------------
# Task 3: persist per-project counts to pipeline_runs.metadata + baseline reader
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, prev_row=None):
        self.executed = []          # list of (sql, params)
        self.executed_fetchrow = [] # list of (sql, params)
        self._prev_row = prev_row

    def execute(self, sql, *params):
        self.executed.append((sql, params))

    def fetchrow(self, sql, *params):
        self.executed_fetchrow.append((sql, params))
        return self._prev_row


def _make_extractor(fake_db):
    ex = BaseExtractor.__new__(BaseExtractor)   # bypass __init__/network
    ex.db = fake_db
    ex._pipeline_name = "asset_tasks_extract"
    ex.run_id = "run-current"
    return ex


def test_complete_pipeline_run_merges_project_counts_into_metadata():
    db = _FakeDB()
    ex = _make_extractor(db)
    ex.complete_pipeline_run("success", 100, error=None, project_counts={"TS13": 100})
    sql, params = db.executed[-1]
    assert "metadata" in sql and "::jsonb" in sql
    # the json payload param must contain project_counts
    assert any('"project_counts"' in p for p in params if isinstance(p, str))
    assert any('"TS13"' in p for p in params if isinstance(p, str))
    assert "WHERE run_id = $6" in sql
    assert len(params) == 6
    assert params[4] == json.dumps({"project_counts": {"TS13": 100}})
    assert params[5] == "run-current"


def test_get_previous_project_counts_parses_metadata():
    db = _FakeDB(prev_row={"records_extracted": 900,
                           "project_counts": {"TS13": 500, "TS16": 400}})
    ex = _make_extractor(db)
    counts, total = ex.get_previous_project_counts()
    assert counts == {"TS13": 500, "TS16": 400}
    assert total == 900
    fsql, fparams = db.executed_fetchrow[-1]
    assert "status = 'success'" in fsql
    assert "run_id <> $2" in fsql
    assert fparams[0] == "asset_tasks_extract"
    assert fparams[1] == "run-current"


def test_get_previous_project_counts_handles_no_prior_run():
    ex = _make_extractor(_FakeDB(prev_row=None))
    assert ex.get_previous_project_counts() == ({}, 0)


# ---------------------------------------------------------------------------
# Task 4: _retry_loop — 3-round retry with rest between rounds
# ---------------------------------------------------------------------------
from extract_asset_tasks import _retry_loop


def test_retry_loop_recovers_in_round_two():
    calls = []
    def retry_once(failed):
        calls.append(list(failed))
        return [] if len(calls) >= 2 else list(failed)  # all recover on round 2
    still = _retry_loop(retry_once, ["TS19"], max_rounds=3, wait_seconds=0, sleep=lambda s: None)
    assert still == []
    assert len(calls) == 2  # stopped early, did not run round 3


def test_retry_loop_exhausts_rounds_when_never_recovers():
    def retry_once(failed):
        return list(failed)  # never recovers
    still = _retry_loop(retry_once, ["TS19"], max_rounds=3, wait_seconds=0, sleep=lambda s: None)
    assert still == ["TS19"]


def test_retry_loop_no_failures_does_nothing():
    def retry_once(failed):
        raise AssertionError("should not be called")
    assert _retry_loop(retry_once, [], wait_seconds=0, sleep=lambda s: None) == []
