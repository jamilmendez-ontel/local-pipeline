"""Tests for QA form auto-discovery decision logic."""
import qa_form_discovery as qa_form_discovery_mod
from qa_form_discovery import (
    match_qa_form,
    missing_ts_numbers,
    needs_escalation,
    register_qa_form,
    run_discovery,
)

FORMS = [
    {"id": "-A1", "title": "ACTIVE - QA Form TS19"},
    {"id": "-A2", "title": "ACTIVE - QA Form TS20"},
    {"id": "-A3", "title": "QA Rejection Form"},
    {"id": "-A4", "title": "Option 1 of QA"},
    {"id": "-A5", "title": "ACTIVE - QA Subcategories - Original DO NOT PULL"},
]


def test_match_exactly_one():
    r = match_qa_form(FORMS, 20)
    assert r["status"] == "one"
    assert r["matches"] == [{"id": "-A2", "title": "ACTIVE - QA Form TS20"}]


def test_match_zero_when_form_absent():
    assert match_qa_form(FORMS, 21)["status"] == "zero"


def test_match_ignores_lookalikes_and_substring_numbers():
    # TS2 must NOT match TS20's title
    assert match_qa_form(FORMS, 2)["status"] == "zero"


def test_match_many_when_duplicated():
    forms = FORMS + [{"id": "-B2", "title": "ACTIVE - QA Form TS20"}]
    r = match_qa_form(forms, 20)
    assert r["status"] == "many"
    assert len(r["matches"]) == 2


def test_missing_ts_numbers():
    projects = [{"project_number": 13}, {"project_number": 19}, {"project_number": 20}]
    registered = {13, 19}
    assert missing_ts_numbers(projects, registered) == [20]


def test_needs_escalation_only_after_7_days_with_tasks():
    assert needs_escalation(task_count=5, project_age_days=8) is True
    assert needs_escalation(task_count=5, project_age_days=3) is False
    assert needs_escalation(task_count=0, project_age_days=30) is False


class _StubDB:
    """Minimal db.fetch/db.execute stub for run_discovery isolation test."""

    def __init__(self, projects, registered):
        self._projects = projects
        self._registered = registered
        self.executed = []

    def fetch(self, query, *args):
        if "ref_ontel_techops_projects" in query:
            return self._projects
        if "ref_qa_forms" in query:
            return [{"ts_number": t} for t in self._registered]
        return []

    def execute(self, query, *args):
        self.executed.append((query, args))


def test_register_qa_form_creates_run_id_and_gin_indexes():
    """RAW_TABLE_DDL must create both indexes every existing raw_form_qa_*
    table has (migrations/004_forms_tables.sql): a run_id b-tree index and
    a GIN(data) index, using the same idx_raw_form_qa_ts{n}_{run_id,data}
    naming convention."""
    db = _StubDB(projects=[], registered=[])

    register_qa_form(db, 22, "-Z1", "ACTIVE - QA Form TS22")

    executed_stmts = [stmt for stmt, _ in db.executed]
    # DDL statements are individually executed (statement-splitting loop) -
    # no statement should have been mangled by a stray semicolon inside a string.
    assert any(
        s.strip().startswith("CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts22_run_id")
        and "ON data_raw.raw_form_qa_ts22(run_id)" in s
        for s in executed_stmts
    ), executed_stmts
    assert any(
        s.strip().startswith("CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts22_data")
        and "USING GIN(data)" in s
        for s in executed_stmts
    ), executed_stmts


def test_run_discovery_isolates_per_ts_failures(monkeypatch):
    # Two unregistered TS projects, both with an exact-match form in Swift.
    monkeypatch.setattr(
        qa_form_discovery_mod,
        "fetch_org_forms",
        lambda token: [
            {"id": "-A2", "title": "ACTIVE - QA Form TS20"},
            {"id": "-A6", "title": "ACTIVE - QA Form TS21"},
        ],
    )

    original_register = qa_form_discovery_mod.register_qa_form
    calls = []

    def flaky_register(db, ts_number, form_id, form_title):
        calls.append(ts_number)
        if ts_number == 20:
            raise RuntimeError("simulated DDL failure for TS20")
        original_register(db, ts_number, form_id, form_title)

    monkeypatch.setattr(qa_form_discovery_mod, "register_qa_form", flaky_register)

    db = _StubDB(
        projects=[{"project_number": 20}, {"project_number": 21}],
        registered=[],
    )

    result = run_discovery(db, token="tok", send_email=False)

    # Both ts numbers were attempted (TS20's failure didn't stop the loop)...
    assert calls == [20, 21]
    # ...and only the one that actually succeeded is reported back to the caller.
    assert result == [21]


def test_run_discovery_alerts_on_pre_loop_infra_failure(monkeypatch):
    """fetch_org_forms 404ing every night (or the pre-loop DB reads failing)
    must not go silent - it should trigger exactly one alert email and
    return cleanly (no exception propagates to the caller / extraction)."""

    def _boom(token):
        raise RuntimeError("simulated Swift 404")

    monkeypatch.setattr(qa_form_discovery_mod, "fetch_org_forms", _boom)

    alerts = []
    monkeypatch.setattr(
        qa_form_discovery_mod,
        "send_alert",
        lambda subject, body: alerts.append((subject, body)),
    )

    db = _StubDB(projects=[{"project_number": 20}], registered=[])

    result = run_discovery(db, token="tok", send_email=True)

    assert result == []
    assert len(alerts) == 1
    subject, body = alerts[0]
    assert "infrastructure failure" in subject
    assert "RuntimeError" in body
    assert "simulated Swift 404" in body


def test_run_discovery_alert_failure_does_not_propagate(monkeypatch):
    """A Gmail failure while sending the infra-failure alert must not itself
    raise out of run_discovery - the alert attempt is isolated in its own
    try/except."""

    def _boom(token):
        raise RuntimeError("simulated Swift 404")

    def _alert_boom(subject, body):
        raise RuntimeError("simulated Gmail failure")

    monkeypatch.setattr(qa_form_discovery_mod, "fetch_org_forms", _boom)
    monkeypatch.setattr(qa_form_discovery_mod, "send_alert", _alert_boom)

    db = _StubDB(projects=[{"project_number": 20}], registered=[])

    # Must not raise.
    result = run_discovery(db, token="tok", send_email=True)
    assert result == []


def test_run_discovery_skips_alert_when_send_email_false_on_infra_failure(monkeypatch):
    """send_email=False (used by tests/dry-runs) must not attempt to send
    the infra-failure alert either."""

    def _boom(token):
        raise RuntimeError("simulated Swift 404")

    alerts = []
    monkeypatch.setattr(qa_form_discovery_mod, "fetch_org_forms", _boom)
    monkeypatch.setattr(
        qa_form_discovery_mod,
        "send_alert",
        lambda subject, body: alerts.append((subject, body)),
    )

    db = _StubDB(projects=[{"project_number": 20}], registered=[])

    result = run_discovery(db, token="tok", send_email=False)

    assert result == []
    assert alerts == []
