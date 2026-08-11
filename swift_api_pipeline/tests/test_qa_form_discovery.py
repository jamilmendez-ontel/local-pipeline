"""Tests for QA form auto-discovery decision logic."""
import qa_form_discovery as qa_form_discovery_mod
from qa_form_discovery import match_qa_form, missing_ts_numbers, needs_escalation, run_discovery

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
