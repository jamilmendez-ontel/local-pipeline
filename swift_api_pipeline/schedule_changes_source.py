"""Google Sheets source for the HR schedule-changes sheet.

Reads every tab of the schedule-changes spreadsheet via the Sheets API v4 and
parses the tabs that match the standard template into ParsedRow records for
sync_schedule_changes.py. Sheet semantics (two record styles, start-year
inference, verbatim notes) are documented in
ai-projects/docs/superpowers/specs/2026-09-02-schedule-change-history.md and
local-pipeline/reference/schedule-changes-sheet.md.

NOT built on sheets_client.py: that client exports via the Drive CSV endpoint,
which only ever returns the FIRST tab (its GCP project has the Sheets API
disabled). This module needs every tab, so it follows the Sheets v4 pattern
from report-automation/weekly-pmi-report/src/sheets_source.py (copied, not
imported; the repos deliberately share no library). The token must therefore
be a Sheets-scoped pickle: env SCHEDULE_SHEETS_TOKEN (locally the PMI intake
token for jamil.mendez@ontel.co works).
"""
from __future__ import annotations

import hashlib
import os
import pickle
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date

SCHEDULE_SHEET_ID = "1yX3D3ykzt8eZx6rMlCnuMf_Ei6hCt9BUh7AdBGi_U4Q"

HEADER_SCAN_ROWS = 15

# Same retry policy as the PMI sheet intake: 429/5xx and socket-level drops are
# worth a fresh attempt; 401/403/404 are permanent and fail immediately.
FETCH_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
FETCH_MAX_ATTEMPTS = 3
FETCH_BACKOFF_BASE = 1.0   # seconds; doubles each retry (1s, 2s)

# Template column order, fixed by the sheet's shared table template.
# find_template_header() verifies the header text before these are trusted.
COL_ID, COL_NAME, COL_ROLE = 0, 1, 2
COL_SS_PHT, COL_SS_ET, COL_SE_PHT, COL_SE_ET = 3, 4, 5, 6
COL_REST, COL_WA, COL_HOURS, COL_SHIFT = 7, 8, 9, 10
COL_START, COL_END, COL_MONTH, COL_YEAR = 11, 12, 13, 14
COL_RDO_TO, COL_RDO_DAY, COL_NOTES = 15, 16, 17

MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# "Mar-9" / "Mar 9" / "March 9" (optionally with an explicit year)
_DATE_MON_DAY = re.compile(r"^\s*([A-Za-z]{3,9})[\s\-]+(\d{1,2})(?:,?\s+(\d{4}))?\s*$")
# "3/9" / "3/9/2026" / "3/9/26"
_DATE_SLASH = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*$")


@dataclass
class ParsedRow:
    sheet_tab: str
    row_index: int              # 0-based row within the tab grid
    id_number: str              # "" when the sheet cell is blank
    member_name: str
    role: str | None
    shift_start_pht: str | None
    shift_end_pht: str | None
    shift_start_et: str | None
    shift_end_et: str | None
    shift_code: str | None      # DS / NS as written
    work_arrangement: str | None
    reg_hours: int | None
    rest_day: str | None
    rdo_to: date | None
    rdo_day: str | None
    start_date: date
    end_date: date | None       # None = open-ended
    change_kind: str            # one_day | temporary | ongoing
    notes: str | None
    raw_cells: list[str]
    row_hash: str


def load_sheets_creds(path: str | None = None):
    """Unpickle Sheets-scoped credentials. No default file in this repo: the
    local sheets_token.pickle here is Drive-scope only and 403s on Sheets v4."""
    token = path or os.environ.get("SCHEDULE_SHEETS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "SCHEDULE_SHEETS_TOKEN is not set. Point it at a Sheets-scoped "
            "token pickle (e.g. report-automation/weekly-pmi-report/"
            "gmail_credentials/sheets_token.pickle).")
    if not os.path.exists(token):
        raise RuntimeError(f"SCHEDULE_SHEETS_TOKEN file not found: {token}")
    # pickle is safe here: the file is a self-minted Google OAuth token written
    # by our own auth flow (house convention for every *_token.pickle in this
    # monorepo), never data from an untrusted source.
    with open(token, "rb") as fh:
        return pickle.load(fh)


def _is_transient(err: Exception) -> bool:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover
        HttpError = ()
    if HttpError and isinstance(err, HttpError):
        status = getattr(getattr(err, "resp", None), "status", None)
        return status in FETCH_RETRYABLE_STATUS
    return isinstance(err, (socket.timeout, TimeoutError, ConnectionError))


def _call_with_retry(fn, what: str, attempts: int = FETCH_MAX_ATTEMPTS):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as err:
            if attempt >= attempts or not _is_transient(err):
                raise
            delay = FETCH_BACKOFF_BASE * (2 ** (attempt - 1))
            print(f"  WARNING: {what} failed (attempt {attempt}/{attempts}): "
                  f"{err}; retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)


def fetch_all_tabs(spreadsheet_id: str, creds) -> list[tuple[str, list[list[str]]]]:
    """Every tab title + full used-range grid (FORMATTED_VALUE). The range is
    the bare quoted tab name on purpose: an A1-bound range silently truncates
    once the sheet grows."""
    from googleapiclient.discovery import build

    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    meta = _call_with_retry(
        lambda: svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title").execute(),
        "Sheets metadata read")
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    out: list[tuple[str, list[list[str]]]] = []
    for title in titles:
        got = _call_with_retry(
            lambda t=title: svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"'{t}'",
                valueRenderOption="FORMATTED_VALUE").execute(),
            f"Sheets tab {title!r} read")
        out.append((title, got.get("values", [])))
    return out


def find_template_header(grid: list[list[str]]) -> int:
    """Row index of the template header, or -1 for non-template tabs.

    The Summary tab (leading 'Cluster' column) is rejected: it mirrors the team
    tabs via formulas, so ingesting it would duplicate every non-TS row."""
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        cells = [str(c).strip() for c in row]
        if not cells:
            continue
        if cells[0] == "Cluster":
            return -1
        if "ID Number" in cells and "Names" in cells and cells[0] == "ID Number":
            return i
    return -1


def parse_sheet_date(text: str, year: int | None) -> date | None:
    """'Mar-9' / 'Jan 16' / '3/9' / '3/9/2026' -> date. An explicit year in the
    text wins over the Year-column argument. Junk/blank/'-' -> None."""
    s = str(text or "").strip()
    if not s or s == "-":
        return None
    m = _DATE_MON_DAY.match(s)
    if m:
        mon = MONTHS.get(m.group(1)[:3].lower())
        if not mon:
            return None
        y = int(m.group(3)) if m.group(3) else year
        if not y:
            return None
        try:
            return date(y, mon, int(m.group(2)))
        except ValueError:
            return None
    m = _DATE_SLASH.match(s)
    if m:
        y = m.group(3)
        if y:
            y = int(y)
            if y < 100:
                y += 2000
        else:
            y = year
        if not y:
            return None
        try:
            return date(int(y), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _clean(cell: str) -> str | None:
    s = str(cell or "").strip()
    return s if s and s != "-" else None


def _row_hash(cells: list[str]) -> str:
    joined = "\x1f".join(str(c).strip() for c in cells)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _non_data_reason(cells: list[str]) -> str | None:
    """Why a non-data row is worth a logged skip, or None for expected noise.

    Expected noise (no log): blank rows, the PHT/EST subheader, repeated
    template header rows mid-tab, and merged PLEASE-READ banner rows. Anything
    row-like that fails _is_data_row (a mangled id cell like '1234.0' or
    '1234 (resigned)', or a shift code that is not DS/NS) is logged so a
    hand-edit never silently drops history."""
    id_cell = str(cells[COL_ID] if len(cells) > COL_ID else "").strip()
    name = str(cells[COL_NAME] if len(cells) > COL_NAME else "").strip()
    shift = str(cells[COL_SHIFT] if len(cells) > COL_SHIFT else "").strip()
    if id_cell in {"", "ID Number"} and not name:
        return None
    if id_cell.upper().startswith("PLEASE READ"):
        return None
    if id_cell == "ID Number":  # repeated header row with a Names cell too
        return None
    if id_cell and not id_cell.isdigit():
        return f"unrecognized id cell {id_cell!r} ({name!r})"
    return f"unrecognized shift code {shift!r} for {name!r}"


def _is_data_row(cells: list[str]) -> bool:
    id_cell = str(cells[COL_ID] if len(cells) > COL_ID else "").strip()
    name = str(cells[COL_NAME] if len(cells) > COL_NAME else "").strip()
    shift = str(cells[COL_SHIFT] if len(cells) > COL_SHIFT else "").strip()
    if id_cell.isdigit():
        return True
    return not id_cell and bool(name) and shift in {"DS", "NS"}


def parse_tab(tab_title: str, grid: list[list[str]]) -> tuple[list[ParsedRow], list[str]]:
    """Parse one template tab. Returns (rows, skip_reasons). Non-template tabs
    should be filtered by the caller via find_template_header first; called on
    one anyway, this returns ([], [])."""
    hdr = find_template_header(grid)
    if hdr < 0:
        return [], []
    rows: list[ParsedRow] = []
    skips: list[str] = []
    for idx in range(hdr + 1, len(grid)):
        cells = [str(c) for c in grid[idx]]
        if not _is_data_row(cells):
            reason = _non_data_reason(cells)
            if reason:
                skips.append(f"{tab_title}!r{idx}: {reason}")
            continue
        # Pad so the fixed column positions are always addressable.
        cells = cells + [""] * (COL_NOTES + 1 - len(cells))
        year_txt = str(cells[COL_YEAR]).strip()
        year = int(year_txt) if year_txt.isdigit() else None
        start = parse_sheet_date(cells[COL_START], year)
        if start is None:
            skips.append(f"{tab_title}!r{idx}: bad start date {cells[COL_START]!r}")
            continue
        end_raw = str(cells[COL_END]).strip()
        end = parse_sheet_date(cells[COL_END], start.year)
        if end is None and end_raw and end_raw != "-":
            # Unparseable end: keep the row as open-ended but leave a trail.
            print(f"  WARNING: {tab_title}!r{idx}: bad end date {end_raw!r}; "
                  f"treating as open-ended", file=sys.stderr)
        if end is not None and end < start:
            # Year column belongs to the start; a "smaller" end crossed Dec 31.
            end = date(start.year + 1, end.month, end.day)
        if end is not None and end == start:
            kind = "one_day"
        elif end is None:
            kind = "ongoing"
        else:
            kind = "temporary"
        hours_txt = str(cells[COL_HOURS]).strip()
        rdo_to = parse_sheet_date(cells[COL_RDO_TO], year)
        if rdo_to is not None and rdo_to < start:
            # Same cross-year rule as end_date: the Year column belongs to the
            # start, so an "earlier" RDO date crossed Dec 31.
            rdo_to = date(start.year + 1, rdo_to.month, rdo_to.day)
        rows.append(ParsedRow(
            sheet_tab=tab_title,
            row_index=idx,
            id_number=str(cells[COL_ID]).strip(),
            member_name=str(cells[COL_NAME]).strip(),
            role=_clean(cells[COL_ROLE]),
            shift_start_pht=_clean(cells[COL_SS_PHT]),
            shift_end_pht=_clean(cells[COL_SE_PHT]),
            shift_start_et=_clean(cells[COL_SS_ET]),
            shift_end_et=_clean(cells[COL_SE_ET]),
            shift_code=_clean(cells[COL_SHIFT]),
            work_arrangement=_clean(cells[COL_WA]),
            reg_hours=int(hours_txt) if hours_txt.isdigit() else None,
            rest_day=_clean(cells[COL_REST]),
            rdo_to=rdo_to,
            rdo_day=_clean(cells[COL_RDO_DAY]),
            start_date=start,
            end_date=end,
            change_kind=kind,
            notes=_clean(cells[COL_NOTES]),
            raw_cells=cells,
            row_hash=_row_hash(cells),
        ))
    return rows, skips
