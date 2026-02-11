#!/usr/bin/env python3
"""
Parser for QuickBooks Sales by Product/Service Detail Excel exports.

The QuickBooks export has a messy format:
- Rows 0-4: header rows (title, as-of date, column headers)
- Product/service group labels appear as group headers in column 0
- Data rows have NaN in column 0 and a date in column 1
- "Total for..." summary rows and "TOTAL" row should be skipped
- Footer row with timestamp should be skipped
- Column order varies between files — parser reads header row (row 4)
  and maps columns by name instead of fixed position
- No group column — product/service headers are skipped (user preference)

Usage:
    as_of_date, rows = parse_sales_excel("path/to/file.xlsx")
"""

import re
from datetime import datetime
from typing import Tuple, List, Dict, Optional

import pandas as pd

# Pattern to detect footer timestamp rows (e.g., "Monday, Feb 09, 2026 10:49:38 PM GMT-8...")
FOOTER_PATTERN = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
    re.IGNORECASE,
)

# Pattern for date range like "February 6-8, 2026" (same month) — extract month, last day, year
DATE_RANGE_SAME_MONTH = re.compile(
    r"(\w+)\s+\d+\s*-\s*(\d+),?\s+(\d{4})"
)

# Pattern for cross-month range like "October 31 - November 2, 2025" — extract last month+day+year
DATE_RANGE_CROSS_MONTH = re.compile(
    r"\w+\s+\d+\s*[-–]\s*(\w+)\s+(\d+),?\s+(\d{4})"
)

# Pattern for month-only like "December 2025" — use last day of month
MONTH_YEAR_PATTERN = re.compile(
    r"^(\w+)\s+(\d{4})$"
)

# Map from header text (lowered) → output field name
COLUMN_MAP = {
    "date": "date",
    "transaction type": "transaction_type",
    "num": "num",
    "customer": "customer",
    "memo/description": "memo_description",
    "qty": "qty",
    "sales price": "sales_price",
    "amount": "amount",
    "balance": "balance",
    "p.o. number": "po_number",
    "service date": "service_date",
}


def parse_header_date(text: str) -> Optional[datetime]:
    """Parse header date — handles both single dates and date ranges.

    Examples:
        'February 9, 2026' → 2026-02-09
        'February 6-8, 2026' → 2026-02-08 (last date in range)
        'October 31 - November 2, 2025' → 2025-11-02
        'December 2025' → 2025-12-31
    """
    text = str(text).strip()

    # Try cross-month range first (e.g., "October 31 - November 2, 2025")
    cross_match = DATE_RANGE_CROSS_MONTH.search(text)
    if cross_match:
        month_str = cross_match.group(1)
        last_day = cross_match.group(2)
        year = cross_match.group(3)
        if not month_str.isdigit():
            date_str = f"{month_str} {last_day}, {year}"
            for fmt in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(date_str, fmt).date()
                except ValueError:
                    continue

    # Try same-month dash range (e.g., "February 6-8, 2026")
    range_match = DATE_RANGE_SAME_MONTH.search(text)
    if range_match:
        month_str = range_match.group(1)
        last_day = range_match.group(2)
        year = range_match.group(3)
        date_str = f"{month_str} {last_day}, {year}"
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue

    # Try month-only format (e.g., "December 2025") — use last day of month
    month_match = MONTH_YEAR_PATTERN.match(text)
    if month_match:
        import calendar
        month_str = month_match.group(1)
        year = int(month_match.group(2))
        for fmt in ("%B", "%b"):
            try:
                month_num = datetime.strptime(month_str, fmt).month
                last_day = calendar.monthrange(year, month_num)[1]
                return datetime(year, month_num, last_day).date()
            except ValueError:
                continue

    # Try single date formats
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def _build_column_index(df) -> Dict[str, int]:
    """Read row 4 (header row) and build a mapping of field_name → column_index.

    Handles varying column orders across QuickBooks export versions.
    """
    col_index = {}
    if len(df) <= 4:
        return col_index

    for col_idx in range(len(df.columns)):
        cell = df.iloc[4, col_idx]
        if pd.isna(cell):
            continue
        header = str(cell).strip().lower()
        if header in COLUMN_MAP:
            col_index[COLUMN_MAP[header]] = col_idx

    return col_index


def parse_sales_excel(filepath: str) -> Tuple[Optional[object], List[Dict]]:
    """
    Parse a QuickBooks Sales by Product/Service Detail Excel file.

    Reads column headers from row 4 and maps by name to handle varying
    column orders across different QuickBooks export versions.

    Args:
        filepath: Path to the Excel file

    Returns:
        Tuple of (as_of_date, list of row dicts)
        Each dict has: date, transaction_type, num, customer, memo_description,
                       qty, sales_price, amount, balance, po_number, service_date
    """
    df = pd.read_excel(filepath, header=None)

    # Extract as_of_date from header rows
    as_of_date = None
    for row_idx in range(min(5, len(df))):
        for col_idx in range(min(3, len(df.columns))):
            cell = df.iloc[row_idx, col_idx]
            if pd.notna(cell):
                as_of_date = parse_header_date(str(cell))
                if as_of_date:
                    break
        if as_of_date:
            break

    if not as_of_date:
        raise ValueError(f"Could not find 'As of' date in {filepath}")

    # Build column index from header row
    col_index = _build_column_index(df)

    # Helper to get a cell value by field name
    def _get(row, field):
        idx = col_index.get(field)
        if idx is None or idx >= len(row):
            return None
        return row.iloc[idx]

    # Process rows — skip header rows (0-4)
    rows = []

    for idx in range(5, len(df)):
        row = df.iloc[idx]
        col0 = row.iloc[0] if len(row) > 0 else None
        col1 = row.iloc[1] if len(row) > 1 else None

        col0_str = str(col0).strip() if pd.notna(col0) else ""

        # Skip "Total for..." rows and "TOTAL" row
        if col0_str.lower().startswith("total"):
            continue

        # Skip footer timestamp rows
        if col0_str and FOOTER_PATTERN.match(col0_str):
            continue

        # Skip group header rows: col 0 has text (product/service name),
        # not a "Total" row, and col 1 is NaN
        if col0_str and pd.isna(col1):
            continue

        # Data rows: col 0 is NaN and col 1 has a date value
        if pd.isna(col0) and pd.notna(col1):
            record = {
                "date": _parse_date(_get(row, "date")),
                "transaction_type": _safe_str(_get(row, "transaction_type")),
                "num": _safe_str(_get(row, "num")),
                "customer": _safe_str(_get(row, "customer")),
                "memo_description": _safe_str(_get(row, "memo_description")),
                "qty": _safe_int(_get(row, "qty")),
                "sales_price": _safe_numeric(_get(row, "sales_price")),
                "amount": _safe_numeric(_get(row, "amount")),
                "balance": _safe_numeric(_get(row, "balance")),
                "po_number": _safe_str(_get(row, "po_number")),
                "service_date": _parse_date(_get(row, "service_date")),
            }
            rows.append(record)

    return as_of_date, rows


def _parse_date(val) -> Optional[str]:
    """Parse a date value to YYYY-MM-DD string."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    val_str = str(val).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _safe_str(val) -> Optional[str]:
    """Convert to string, returning None for NaN/empty."""
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def _safe_numeric(val) -> Optional[float]:
    """Convert to float, returning None for NaN."""
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    """Convert to int, returning None for NaN."""
    if val is None or pd.isna(val):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parse_sales.py <excel_file>")
        sys.exit(1)

    filepath = sys.argv[1]
    as_of_date, rows = parse_sales_excel(filepath)
    print(f"As of date: {as_of_date}")
    print(f"Total rows: {len(rows)}")

    if rows:
        print(f"\nFirst row: {rows[0]}")
        print(f"Last row: {rows[-1]}")
