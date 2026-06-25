# swift_api_pipeline/tests/test_asset_tasks_resilience.py
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline_notifier import PipelineOutcome
from extract_asset_tasks import detect_abnormal_counts, ABNORMAL_DROP_PCT, MAX_RETRY_ROUNDS


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
