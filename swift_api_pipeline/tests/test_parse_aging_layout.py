"""parse_aging maps columns by header name and refuses files missing a required column.

Layouts mirror real QuickBooks exports from the "Daily Revenue Report" emails:
- normal (every day but two): ... Open Balance | Past Due | P.O. Number
- 2026-10-27: ... Open Balance | Memo/Description | Past Due   (no PO column)
- 2026-10-30: ... Open Balance | Client/Vendor Message | P.O. Number   (no Past Due)
The old positional parser loaded both bad files with Past Due blank, which zeroed
the Daily Finance report's Overdue and Pending PO numbers for those days.
"""

import pandas as pd
import pytest

from parse_aging import AgingLayoutError, parse_aging_excel

BASE = ["Date", "Transaction Type", "Num", "Customer", "Location", "Due Date",
        "Amount", "Open Balance"]


def _write(tmp_path, tail_headers, rows, as_of="As of October 27, 2026"):
    """Build a QuickBooks-shaped sheet: 3 title rows, blank, header row, then groups."""
    width = 1 + len(BASE) + len(tail_headers)
    blank = [None] * width
    sheet = [
        ["ONTEL LLC"] + blank[1:],
        ["A/R Aging Detail"] + blank[1:],
        [as_of] + blank[1:],
        list(blank),
        [None] + BASE + tail_headers,
        ["31 - 60 days past due"] + blank[1:],
    ]
    for r in rows:
        sheet.append([None] + r)
    sheet.append(["Total for 31 - 60 days past due"] + blank[1:])
    path = tmp_path / "aging.xlsx"
    pd.DataFrame(sheet).to_excel(path, header=False, index=False)
    return str(path)


ROW = ["09/01/2026", "Invoice", "63931", "Saturn Business Systems, Inc.", "NY",
       "09/27/2026", 300, 300]


def test_normal_layout(tmp_path):
    path = _write(tmp_path, ["Past Due", "P.O. Number"], [ROW + [30, "Requested 07/29/2026"]])
    as_of, rows = parse_aging_excel(path)
    assert str(as_of) == "2026-10-27"
    assert len(rows) == 1
    r = rows[0]
    assert r["past_due"] == 30
    assert r["po_number"] == "Requested 07/29/2026"
    assert r["open_balance"] == 300
    assert r["due_date"] == "2026-09-27"
    assert r["aging_bucket"] == "31-60 days"


def test_columns_found_by_name_not_position(tmp_path):
    # Same data, extra column in the middle and the last two swapped.
    path = _write(tmp_path, ["Memo/Description", "P.O. Number", "Past Due"],
                  [ROW + ["Revised", "121889", 30]])
    _, rows = parse_aging_excel(path)
    assert rows[0]["past_due"] == 30
    assert rows[0]["po_number"] == "121889"


def test_oct27_layout_missing_po_is_rejected(tmp_path):
    path = _write(tmp_path, ["Memo/Description", "Past Due"], [ROW + ["Revised", 30]])
    with pytest.raises(AgingLayoutError) as exc:
        parse_aging_excel(path)
    msg = str(exc.value)
    assert "P.O. Number" in msg
    assert "Memo/Description" in msg  # names what the file had instead


def test_oct30_layout_missing_past_due_is_rejected(tmp_path):
    path = _write(tmp_path, ["Client/Vendor Message", "P.O. Number"],
                  [ROW + ["124629", "Requested 09/16/2026 (SM)"]])
    with pytest.raises(AgingLayoutError) as exc:
        parse_aging_excel(path)
    assert "Past Due" in str(exc.value)


def test_header_aliases_and_spacing(tmp_path):
    path = _write(tmp_path, ["  past   due ", "PO Number"], [ROW + [30, "121889"]])
    _, rows = parse_aging_excel(path)
    assert rows[0]["past_due"] == 30
    assert rows[0]["po_number"] == "121889"


def test_location_is_optional(tmp_path):
    width_rows = [ROW[:4] + ROW[5:] + [30, "121889"]]
    # Rebuild without the Location column.
    base = [h for h in BASE if h != "Location"]
    blank = [None] * (1 + len(base) + 2)
    sheet = [["ONTEL LLC"] + blank[1:], ["A/R Aging Detail"] + blank[1:],
             ["As of October 27, 2026"] + blank[1:], list(blank),
             [None] + base + ["Past Due", "P.O. Number"],
             ["Current"] + blank[1:]] + [[None] + r for r in width_rows]
    path = tmp_path / "noloc.xlsx"
    pd.DataFrame(sheet).to_excel(path, header=False, index=False)
    _, rows = parse_aging_excel(str(path))
    assert rows[0]["location"] is None
    assert rows[0]["past_due"] == 30
    assert rows[0]["aging_bucket"] == "Current"


def test_no_header_row_is_rejected(tmp_path):
    sheet = [["ONTEL LLC"], ["A/R Aging Detail"], ["As of October 27, 2026"], [None], ["junk"]]
    path = tmp_path / "nohdr.xlsx"
    pd.DataFrame(sheet).to_excel(path, header=False, index=False)
    with pytest.raises(AgingLayoutError):
        parse_aging_excel(str(path))
