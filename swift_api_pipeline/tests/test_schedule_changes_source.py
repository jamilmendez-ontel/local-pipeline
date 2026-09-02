"""Parser tests for the HR schedule-changes sheet source.

Fixture rows are real rows verified against the live sheet on 2026-09-02
(spec: ai-projects/docs/superpowers/specs/2026-09-02-schedule-change-history.md).
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_changes_source import (  # noqa: E402
    find_template_header,
    parse_sheet_date,
    parse_tab,
)

HEADER = ["ID Number", "Names", "Role", "Shift Start", "Shift Start", "Shift End",
          "Shift End", "Rest Day", "Work Arrangement", "Reg Hours", "Shift",
          "Start Date", "End Date", "Month", "Year", "RDO To", "Day", "Notes"]
SUBHEADER = ["", "", "", "PHT", "EST", "PHT", "EST", "", "", "", "", "", "", "",
             "", "", "", ""]


def _grid(rows):
    return [["PLEASE READ  Please don't change the template of this table."],
            HEADER, SUBHEADER] + rows


JAMIL_ONGOING = ["250901", "Jamil Mendez", "DA", "1 PM", "1 AM", "10 PM", "10 AM",
                 "-", "5DWW", "9", "DS", "Sep-22", "-", "Sep - -", "2025", "", "",
                 "New schedule starting Sept 22 (with approval from Merj)"]
JAMIL_ONE_DAY = ["250901", "Jamil Mendez", "DA", "6 AM", "6 PM", "3 PM", "3 AM",
                 "-", "5DWW", "9", "DS", "Sep-3", "Sep-3", "Sep - Sep", "2026", "",
                 "Thursday", "Shift adjustment just for Sept 3"]
CROSS_YEAR = ["241001", "Kyla Palo", "Acct Admin", "1 PM", "1 AM", "10 PM", "10 AM",
              "-", "5DWW", "9", "DS", "Oct-20", "Jan-1", "Oct - Jan", "2025", "",
              "", "Shift starts at 9 am every Monday and Friday."]


def test_header_found_on_template_tab():
    assert find_template_header(_grid([])) == 1


def test_summary_tab_is_rejected():
    grid = [["Cluster"] + HEADER]
    assert find_template_header(grid) == -1


def test_non_template_tab_is_rejected():
    grid = [["ID Number", "Swift Name", "ID Number", "Position"],
            ["1", "", "1", "CEO and President"]]
    assert find_template_header(grid) == -1


def test_parse_sheet_date_dash_form():
    assert parse_sheet_date("Mar-9", 2026) == date(2026, 3, 9)


def test_parse_sheet_date_space_form():
    assert parse_sheet_date("Jan 16", 2026) == date(2026, 1, 16)


def test_parse_sheet_date_slash_forms():
    assert parse_sheet_date("3/9", 2026) == date(2026, 3, 9)
    assert parse_sheet_date("3/9/2026", 2025) == date(2026, 3, 9)  # explicit year wins


def test_parse_sheet_date_junk_is_none():
    assert parse_sheet_date("-", 2026) is None
    assert parse_sheet_date("", 2026) is None
    assert parse_sheet_date("???", 2026) is None


def test_ongoing_row_parses():
    rows, skips = parse_tab("DA", _grid([JAMIL_ONGOING]))
    assert skips == []
    r = rows[0]
    assert (r.id_number, r.change_kind, r.end_date) == ("250901", "ongoing", None)
    assert r.start_date == date(2025, 9, 22)
    assert r.shift_start_pht == "1 PM" and r.shift_start_et == "1 AM"
    assert r.shift_code == "DS" and r.work_arrangement == "5DWW" and r.reg_hours == 9
    assert r.rest_day is None  # "-" normalizes to None
    assert r.notes.startswith("New schedule")


def test_one_day_row_parses():
    rows, _ = parse_tab("DA", _grid([JAMIL_ONE_DAY]))
    assert rows[0].change_kind == "one_day"
    assert rows[0].start_date == rows[0].end_date == date(2026, 9, 3)
    assert rows[0].rdo_day == "Thursday"


def test_cross_year_end_gets_next_year():
    rows, _ = parse_tab("Accounting", _grid([CROSS_YEAR]))
    assert rows[0].start_date == date(2025, 10, 20)
    assert rows[0].end_date == date(2026, 1, 1)
    assert rows[0].change_kind == "temporary"


def test_bad_start_date_row_is_skipped_with_reason():
    bad = list(JAMIL_ONGOING)
    bad[11] = "???"
    rows, skips = parse_tab("DA", _grid([bad]))
    assert rows == []
    assert len(skips) == 1 and "bad start date" in skips[0]


def test_blank_id_row_with_shift_is_kept_for_name_matching():
    noid = list(JAMIL_ONGOING)
    noid[0] = "  "
    rows, _ = parse_tab("DA", _grid([noid]))
    assert rows[0].id_number == ""


def test_subheader_row_is_not_data():
    rows, skips = parse_tab("DA", _grid([]))
    assert rows == [] and skips == []


def test_row_hash_stable_and_distinct():
    a, _ = parse_tab("DA", _grid([JAMIL_ONGOING]))
    b, _ = parse_tab("DA", _grid([JAMIL_ONGOING]))
    c, _ = parse_tab("DA", _grid([JAMIL_ONE_DAY]))
    assert a[0].row_hash == b[0].row_hash
    assert a[0].row_hash != c[0].row_hash


def test_row_index_points_at_grid_row():
    rows, _ = parse_tab("DA", _grid([JAMIL_ONGOING, JAMIL_ONE_DAY]))
    assert [r.row_index for r in rows] == [3, 4]
