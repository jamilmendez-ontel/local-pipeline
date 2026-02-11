#!/usr/bin/env python3
"""
Parser for QuickBooks AR Aging Detail Excel exports.

The QuickBooks export has a messy format:
- Rows 0-4: header rows (title, as-of date, column headers)
- Aging bucket labels appear as group headers in column 0
- Data rows have NaN in column 0 and a date in column 1
- "Total for..." summary rows and "TOTAL" row should be skipped
- 10 data columns: Date, Transaction Type, Num, Customer, Location,
  Due Date, Amount, Open Balance, Past Due, P.O. Number

Usage:
    as_of_date, rows = parse_aging_excel("path/to/file.xlsx")
"""

import re
from datetime import datetime
from typing import Tuple, List, Dict, Optional

import pandas as pd

# Aging bucket labels as they appear in the QuickBooks export
AGING_BUCKET_LABELS = {
    "91 or more days past due": "91+ days",
    "61 - 90 days past due": "61-90 days",
    "31 - 60 days past due": "31-60 days",
    "1 - 30 days past due": "1-30 days",
    "Current": "Current",
}

# Pattern to extract the "As of" date from the header
AS_OF_PATTERN = re.compile(r"As of\s+(.+)", re.IGNORECASE)


def parse_as_of_date(text: str) -> Optional[datetime]:
    """Parse 'As of February 2, 2026' → date object."""
    match = AS_OF_PATTERN.search(str(text))
    if not match:
        return None
    date_str = match.group(1).strip()
    # Try common formats
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def parse_aging_excel(filepath: str) -> Tuple[Optional[object], List[Dict]]:
    """
    Parse a QuickBooks AR Aging Detail Excel file.

    Args:
        filepath: Path to the Excel file

    Returns:
        Tuple of (as_of_date, list of row dicts)
        Each dict has: date, transaction_type, num, customer, location,
                       due_date, amount, open_balance, past_due, po_number,
                       aging_bucket
    """
    # Read without header inference — the file is too messy for pandas auto-detect
    df = pd.read_excel(filepath, header=None)

    # Extract as_of_date from row index 2 (third row)
    # Try rows 1-4 since exact position can vary slightly
    as_of_date = None
    for row_idx in range(min(5, len(df))):
        for col_idx in range(min(3, len(df.columns))):
            cell = df.iloc[row_idx, col_idx]
            if pd.notna(cell):
                as_of_date = parse_as_of_date(str(cell))
                if as_of_date:
                    break
        if as_of_date:
            break

    if not as_of_date:
        raise ValueError(f"Could not find 'As of' date in {filepath}")

    # Process rows — skip header rows (0-4)
    rows = []
    current_bucket = None

    for idx in range(5, len(df)):
        row = df.iloc[idx]
        col0 = row.iloc[0] if len(row) > 0 else None
        col1 = row.iloc[1] if len(row) > 1 else None

        col0_str = str(col0).strip() if pd.notna(col0) else ""

        # Check if this row is an aging bucket header
        bucket_match = None
        for label, bucket_name in AGING_BUCKET_LABELS.items():
            if col0_str.lower() == label.lower():
                bucket_match = bucket_name
                break
        if bucket_match:
            current_bucket = bucket_match
            continue

        # Skip "Total for..." rows and "TOTAL" row
        if col0_str.lower().startswith("total"):
            continue

        # Data rows: col 0 is NaN and col 1 has a date value
        if pd.isna(col0) and pd.notna(col1):
            # Parse the data columns
            record = {
                "date": _parse_date(col1),
                "transaction_type": _safe_str(row.iloc[2] if len(row) > 2 else None),
                "num": _safe_str(row.iloc[3] if len(row) > 3 else None),
                "customer": _safe_str(row.iloc[4] if len(row) > 4 else None),
                "location": _safe_str(row.iloc[5] if len(row) > 5 else None),
                "due_date": _parse_date(row.iloc[6] if len(row) > 6 else None),
                "amount": _safe_numeric(row.iloc[7] if len(row) > 7 else None),
                "open_balance": _safe_numeric(row.iloc[8] if len(row) > 8 else None),
                "past_due": _safe_int(row.iloc[9] if len(row) > 9 else None),
                "po_number": _safe_str(row.iloc[10] if len(row) > 10 else None),
                "aging_bucket": current_bucket,
            }
            rows.append(record)

    return as_of_date, rows


def _parse_date(val) -> Optional[str]:
    """Parse a date value to YYYY-MM-DD string."""
    if pd.isna(val):
        return None
    # Handle pandas Timestamp
    if isinstance(val, pd.Timestamp):
        return val.strftime("%Y-%m-%d")
    # Handle datetime
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    # Try string parsing
    val_str = str(val).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return val_str


def _safe_str(val) -> Optional[str]:
    """Convert to string, returning None for NaN/empty."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def _safe_numeric(val) -> Optional[float]:
    """Convert to float, returning None for NaN."""
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    """Convert to int, returning None for NaN."""
    if pd.isna(val):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parse_aging.py <excel_file>")
        sys.exit(1)

    filepath = sys.argv[1]
    as_of_date, rows = parse_aging_excel(filepath)
    print(f"As of date: {as_of_date}")
    print(f"Total rows: {len(rows)}")

    # Show bucket distribution
    buckets = {}
    for row in rows:
        bucket = row["aging_bucket"] or "Unknown"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    print("\nRows by aging bucket:")
    for bucket, count in sorted(buckets.items()):
        print(f"  {bucket}: {count}")
