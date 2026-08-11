"""Tests for qa_forms_registry (DB-backed replacement of config.QA_FORMS)."""
from qa_forms_registry import load_qa_forms, row_to_entry


class FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None

    def fetch(self, query, *args):
        self.last_query = " ".join(query.split())
        return self.rows


def test_row_to_entry_derives_key_table_and_display_name():
    key, entry = row_to_entry(20, "-Oxyz12345678901234")
    assert key == "qa_ts20"
    assert entry == {
        "form_id": "-Oxyz12345678901234",
        "table_name": "raw_form_qa_ts20",
        "display_name": "QA Form TS20",
    }


def test_load_qa_forms_returns_legacy_shape_ordered():
    rows = [
        {"ts_number": 13, "form_id": "-A"},
        {"ts_number": 20, "form_id": "-B"},
    ]
    db = FakeDb(rows)
    forms = load_qa_forms(db)
    assert list(forms.keys()) == ["qa_ts13", "qa_ts20"]
    assert forms["qa_ts13"]["table_name"] == "raw_form_qa_ts13"
    assert "ref_qa_forms" in db.last_query
    assert "active" in db.last_query


def test_load_qa_forms_empty_registry_raises():
    import pytest
    db = FakeDb([])
    try:
        load_qa_forms(db)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ref_qa_forms" in str(e)
