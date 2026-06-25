# swift_api_pipeline/tests/test_asset_tasks_resilience.py
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline_notifier import PipelineOutcome


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
