"""notify_workflow_failure: the failed (post-extract) and cancelled (timeout) alerts."""
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import notify_workflow_failure as nwf  # noqa: E402


def _parse(argv):
    calls = {}

    def fake_send(**kw):
        calls.update(kw)

    with mock.patch.object(nwf, "send_pipeline_email", side_effect=fake_send):
        rc = nwf.main(argv)
    return rc, calls


def test_cancelled_reason_names_timeout_and_recovery():
    rc, calls = _parse(["--workflow", "Pipeline: Asset Tasks", "--downstream", "all",
                        "--reason", "cancelled", "--run-url", "https://x/runs/1"])
    assert rc == 0
    r = calls["results"][0]
    assert r.status == "FAILED"
    assert "timed out" in r.pipeline_name
    assert "timeout-minutes" in r.error_message
    assert "stage=transform" in r.error_message
    assert "https://x/runs/1" in r.error_message
    assert calls["run_label"].endswith("CANCELLED/TIMEOUT")
    assert calls["overall_status"] == "FAILED"


def test_default_reason_is_post_extract_failure_unchanged():
    rc, calls = _parse(["--workflow", "Pipeline: Asset Tasks", "--downstream", "validator"])
    r = calls["results"][0]
    assert rc == 0
    assert r.pipeline_name == "Pipeline: Asset Tasks (post-extract step)"
    assert "date-validator-daily was NOT dispatched" in r.error_message
    assert calls["run_label"] == "Pipeline: Asset Tasks [validator]"


def test_send_error_never_fails_the_step():
    with mock.patch.object(nwf, "send_pipeline_email", side_effect=RuntimeError("smtp")):
        assert nwf.main(["--workflow", "W", "--reason", "cancelled"]) == 0
