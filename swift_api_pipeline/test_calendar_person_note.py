"""Unit tests for Wave 2 person_note normalization. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_person_note.py -v
"""
import pytest

from calendar_normalize import normalize_person_note


# --- the spec's headline case: four casings of a window collapse to one ---

@pytest.mark.parametrize("raw", ["2pm onwards", "2PM onwards", "2PM Onwards",
                                 "2 PM Onwards", "2 pm onwards", "2pm Onwards"])
def test_onwards_casings_collapse(raw):
    assert normalize_person_note(raw) == "2:00 PM onwards"


@pytest.mark.parametrize("raw, want", [
    ("1:30 PM Onwards", "1:30 PM onwards"),
    ("2:30PM Onwards", "2:30 PM onwards"),
    ("12nn onwards", "12:00 PM onwards"),
    ("16:00 onwards", "4:00 PM onwards"),       # 24-hour clock
    ("12:40 Onwards", "12:40 PM onwards"),      # no meridiem, workday rule
    ("11 am Onwards", "11:00 AM onwards"),
    ("Working 12PM onwards", "12:00 PM onwards"),
    ("working 1 PM onwards", "1:00 PM onwards"),
    ("Working  11:30 AM onwards", "11:30 AM onwards"),
    ("Twerking 1PM onwards", "1:00 PM onwards"),  # typo of Working in the source
    ("After 4 PM", "4:00 PM onwards"),
    ("In by 12PM", "12:00 PM onwards"),
    ("In 10:30 AM", "10:30 AM onwards"),
    ("Available by 11:30 AM", "11:30 AM onwards"),
    ("Available 1:30 PM", "1:30 PM onwards"),
    ("will work at 12 pm", "12:00 PM onwards"),
    ("Reports at 1PM", "1:00 PM onwards"),
    ("Working 12 PM", "12:00 PM onwards"),
    ("11AM PHT Onwards", "11:00 AM PHT onwards"),
])
def test_start_time_forms(raw, want):
    assert normalize_person_note(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Until 12 PM", "Until 12:00 PM"),
    ("Until 12PM", "Until 12:00 PM"),
    ("working until 12NN", "Until 12:00 PM"),
    ("until 2pm only", "Until 2:00 PM"),
    ("only until 1pm", "Until 1:00 PM"),
    ("Working until 11:30 AM", "Until 11:30 AM"),
    ("Out 2PM", "Until 2:00 PM"),
])
def test_until_forms(raw, want):
    assert normalize_person_note(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("9AM - 2PM", "9:00 AM - 2:00 PM"),
    ("9am to 2pm", "9:00 AM - 2:00 PM"),
    ("9:00AM to 2:00PM", "9:00 AM - 2:00 PM"),
    ("9 am to 2 pm", "9:00 AM - 2:00 PM"),
    ("9am-12nn", "9:00 AM - 12:00 PM"),
    ("2-6PM", "2:00 PM - 6:00 PM"),       # start inherits end's PM
    ("7-9AM", "7:00 AM - 9:00 AM"),       # start inherits end's AM
    ("9-1 PM", "9:00 AM - 1:00 PM"),      # start > end, so the other meridiem
    ("2-6", "2:00 PM - 6:00 PM"),         # both bare: workday rule
    ("9-2", "9:00 AM - 2:00 PM"),
    ("2 - 6pm", "2:00 PM - 6:00 PM"),
    ("10AM to 3PM EST", "10:00 AM - 3:00 PM EST"),
    ("3:30PM - 7PM", "3:30 PM - 7:00 PM"),
])
def test_ranges(raw, want):
    assert normalize_person_note(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("2 pm", "2:00 PM"), ("3PM", "3:00 PM"), ("8AM", "8:00 AM"), ("11am", "11:00 AM"),
])
def test_bare_time(raw, want):
    assert normalize_person_note(raw) == want


# --- employee-id suffixes are identifiers, not notes ---

@pytest.mark.parametrize("raw", ["200601", "220323", "-260501", "2202325"])
def test_employee_id_suffix_is_not_a_note(raw):
    assert normalize_person_note(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "(2)"])
def test_empty_and_noise(raw):
    assert normalize_person_note(raw) is None


# --- pass-through: casing standardized only ---

@pytest.mark.parametrize("raw, want", [
    ("Weekend Work", "Weekend work"),
    ("Working AM", "Working AM"),
    ("AM", "AM"),
    ("LAC", "LAC"),
    ("Half day", "Half day"),
    ("Half Day", "Half day"),
    ("halfday", "Half day"),
    ("working 1st Half", "Working 1st half"),
    ("working 2nd Half", "Working 2nd half"),
    ("onwards", "Onwards"),
    ("Annual Performance Evaluation", "Annual performance evaluation"),
])
def test_pass_through(raw, want):
    assert normalize_person_note(raw) == want


def test_idempotent_on_its_own_output():
    for raw in ["2pm onwards", "9-1 PM", "until 2pm only", "Weekend Work", "11AM PHT Onwards"]:
        once = normalize_person_note(raw)
        assert normalize_person_note(once) == once


# --- wiring: the pipeline and the backfill actually write the column ---

def test_load_staging_fills_person_note_normalized():
    from calendar_events_load import load_staging, _UPSERT_COLS
    from test_calendar_events_load import FakeDB, _shape
    db = FakeDB()
    shape = dict(_shape(), person_note="2PM Onwards")
    ev = {"id": "e1", "summary": "VL - Zeta - Luis - 2PM Onwards", "status": "confirmed",
          "start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"},
          "created": "2026-01-01T00:00:00Z", "updated": "2026-01-02T00:00:00Z",
          "creator": {"email": "a@ontel.co"}}
    load_staging(db, "run-1", [ev], resolve_fn=lambda d, s: shape)
    row = db.upserts[0]
    assert row[_UPSERT_COLS.index("person_note")] == "2PM Onwards"
    assert row[_UPSERT_COLS.index("person_note_normalized")] == "2:00 PM onwards"


class _RenormDB:
    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def fetch(self, q, *a):
        return self.rows if "FROM data_staging.stg_calendar_events" in q else []

    def execute(self, q, *a):
        self.updates.append((q, a))

    def executemany(self, q, a):
        pass


def test_renormalize_writes_person_note_normalized():
    from extract_calendar_events import renormalize
    db = _RenormDB([
        {"event_id": "a", "leave_type": "VL", "team": None, "person": None, "person_note": "9am to 2pm"},
        {"event_id": "b", "leave_type": "VL", "team": None, "person": None, "person_note": "200601"},
    ])
    renormalize(db)
    q, args = db.updates[-1]
    assert "person_note_normalized = d.pnn" in q
    assert args[0] == ["a", "b"]
    assert args[-1] == ["9:00 AM - 2:00 PM", None]


def test_backfill_plan_only_changes_differing_rows():
    from _backfill_person_note import plan
    rows = [
        {"event_id": "a", "person_note": "2pm onwards", "current": None},
        {"event_id": "b", "person_note": "2pm onwards", "current": "2:00 PM onwards"},
        {"event_id": "c", "person_note": "200601", "current": None},
        {"event_id": "d", "person_note": None, "current": None},
    ]
    changes, stats = plan(rows)
    assert [c[0] for c in changes] == ["a"]
    assert stats["with_note"] == 3
    assert stats["would_have_value"] == 2
    assert stats["note_to_null"] == 1
