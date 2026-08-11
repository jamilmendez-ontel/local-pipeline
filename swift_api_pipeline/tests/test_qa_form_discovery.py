"""Tests for QA form auto-discovery decision logic."""
from qa_form_discovery import match_qa_form, missing_ts_numbers, needs_escalation

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
