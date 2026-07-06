#!/usr/bin/env python3
"""Roster gap watcher — detect people missing from the employee reference,
self-heal what the data supports, and ping Jamil on Google Chat for the rest.

What it does each run:
    1. Finds emp_ids that SUBMITTED daily reports recently but have no
       reference.ref_employees row (new hires nobody added to the roster sheet).
    2. For each, infers what the warehouse supports — display name (from the
       task asset name), timer email, hire date (first timer entry / first
       submitted work date), carrier group (from approver-group routing) —
       and APPENDS a row to the roster Google Sheet, then runs the employee
       sync so ref_employees picks it up immediately. Fields it cannot infer
       (position, legal name, employment status, ...) are left blank for HR;
       filling them in the sheet flows to the DB on the next sync.
    3. Finds recent timer emails that match no roster email (identity
       mismatches like ronald@ vs ton@). These cannot be fixed automatically.
    4. Posts one Google Chat message summarizing 2+3 and asking for the
       missing info (same Chat REST API pattern as the Open Items report;
       posts as the notifier user). Silent when there is nothing to report.

Usage:
    python roster_gap_watcher.py            # dry run: print, change nothing
    python roster_gap_watcher.py --apply    # append + sync + chat
    python roster_gap_watcher.py --apply --no-chat
    ROSTER_WATCH_CHAT_SPACE=spaces/XXX ...  # target space (dormant if unset)
"""

import argparse
import os
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

from googleapiclient.discovery import build

from config import get_logger, get_db, close_db, retry_db, setup_logging
from sync_employees import SHEET_ID, read_sheet, sync

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

setup_logging()
logger = get_logger("roster_gap_watcher")

_BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = _BASE_DIR / "gmail_credentials"
SHEETS_RW_TOKEN = CREDENTIALS_DIR / "sheets_rw_token.pickle"
CHAT_TOKEN = CREDENTIALS_DIR / "chat_token.pickle"

LOOKBACK_DAYS = 14

# Sheet column order (must match the roster sheet header row).
SHEET_COLUMNS = [
    "emp_id", "last_name", "first_name", "middle_name", "full_name",
    "nickname", "email", "position", "role2", "carrier", "carrier_group",
    "cluster", "division", "sub_division", "work_schedule", "shift_schedule",
    "employment_status", "hire_date", "is_active", "resignation_date",
    "effective_date", "change_reason",
]

# Approver-group routing -> roster carrier_group values.
CARRIER_GROUP_BY_APPROVER = {
    "CG1": "CG1 - Verizon",
    "CG2": "CG2 - AT&T/DISH",
    "CG3": "CG3 - TMO/USCC",
}


def _load_token(path: Path):
    # Safe: these pickles are OAuth tokens we minted ourselves; same convention
    # as every other token in gmail_credentials/.
    with open(path, "rb") as f:
        return pickle.load(f)


def find_missing_reporters(db):
    """emp_ids with recently submitted reports but no ref_employees row."""
    return retry_db(lambda: db.fetch(
        """
        SELECT t.emp_id,
               MAX(t.asset_name) AS asset_name,
               (SELECT MIN(s.work_date) FROM data_staging.stg_daily_reports s
                WHERE s.emp_id = t.emp_id AND s.submitted_on IS NOT NULL) AS first_submitted_wd,
               MAX(t.submitted_on) AS last_submitted,
               COUNT(*) FILTER (WHERE t.submitted_on IS NOT NULL) AS submitted_count
        FROM data_staging.stg_daily_reports t
        LEFT JOIN reference.ref_employees e ON e.emp_id = t.emp_id
        WHERE e.emp_id IS NULL
          AND t.emp_id IS NOT NULL
          AND t.submitted_on >= now() - ($1 || ' days')::interval
        GROUP BY t.emp_id
        ORDER BY t.emp_id
        """,
        str(LOOKBACK_DAYS),
    ))


def find_orphan_timer_emails(db):
    """Recent timer emails that match no roster email (identity mismatches)."""
    return retry_db(lambda: db.fetch(
        """
        WITH current_emp AS (
            SELECT DISTINCT ON (emp_id) lower(email) AS email
            FROM reference.ref_employees
            ORDER BY emp_id, effective_date DESC
        )
        SELECT lower(t.user_email) AS email,
               MAX(t.user_name) AS user_name,
               COUNT(*) AS entries,
               MIN(t.start_time)::date AS first_entry,
               MAX(t.start_time)::date AS last_entry
        FROM data_staging.stg_timer_activities_clean t
        WHERE t.start_time >= now() - ($1 || ' days')::interval
          AND t.user_email LIKE '%@%'
          AND lower(t.user_email) NOT IN
              (SELECT email FROM current_emp WHERE email IS NOT NULL)
        GROUP BY lower(t.user_email)
        ORDER BY last_entry DESC
        """,
        str(LOOKBACK_DAYS),
    ))


def infer_carrier_group(db, emp_id: str):
    """Carrier group from 'Daily Report Approvers - CGx' routing, if any."""
    row = retry_db(lambda: db.fetchrow(
        """
        SELECT assigned_approver, COUNT(*) AS n
        FROM data_staging.stg_daily_reports
        WHERE emp_id = $1 AND assigned_approver LIKE 'Daily Report Approvers - CG%'
        GROUP BY assigned_approver ORDER BY n DESC LIMIT 1
        """,
        emp_id,
    ))
    if not row:
        return ""
    code = row["assigned_approver"].rsplit(" ", 1)[-1]  # "CG3"
    return CARRIER_GROUP_BY_APPROVER.get(code, "")


def display_name_from_asset(asset_name: str, emp_id: str) -> str:
    """Mirror the analytics view's fallback: '<Name>_<emp_id>' -> '<Name>'."""
    if asset_name and emp_id and asset_name.endswith(f"_{emp_id}"):
        return asset_name[: -(len(emp_id) + 1)]
    return ""


def match_timer_identity(db, display_name: str):
    """Find a timer email whose user_name equals the display name (exact,
    case-insensitive). Returns (email, first_entry_date) or ("", None)."""
    if not display_name:
        return "", None
    row = retry_db(lambda: db.fetchrow(
        """
        SELECT lower(user_email) AS email, MIN(start_time)::date AS first_entry
        FROM data_staging.stg_timer_activities_clean
        WHERE lower(trim(user_name)) = lower(trim($1))
          AND user_email LIKE '%@%'
        GROUP BY lower(user_email)
        ORDER BY MAX(start_time) DESC
        LIMIT 1
        """,
        display_name,
    ))
    if not row:
        return "", None
    return row["email"], row["first_entry"]


def build_sheet_row(db, gap) -> dict:
    """Assemble a best-effort roster row for one missing reporter."""
    emp_id = gap["emp_id"]
    name = display_name_from_asset(gap["asset_name"] or "", emp_id)
    email, first_timer = match_timer_identity(db, name)

    hire_candidates = [d for d in (first_timer, gap["first_submitted_wd"]) if d]
    hire = min(hire_candidates) if hire_candidates else None
    hire_str = f"{hire.month}/{hire.day}/{hire.year}" if hire else ""

    parts = name.split()
    row = {c: "" for c in SHEET_COLUMNS}
    row.update({
        "emp_id": emp_id,
        "last_name": parts[-1] if len(parts) > 1 else "",
        "first_name": " ".join(parts[:-1]) if len(parts) > 1 else name,
        "full_name": name or f"Unknown ({emp_id})",
        "nickname": parts[0] if parts else "",
        "email": email,
        "carrier_group": infer_carrier_group(db, emp_id),
        "hire_date": hire_str,
        "is_active": "TRUE",
        "effective_date": hire_str,
        "change_reason": f"Auto-added by roster watcher {date.today().isoformat()} - needs HR completion",
    })
    return row


def append_rows_to_sheet(rows: list) -> None:
    """Append roster rows to the sheet via the Sheets API (RW token)."""
    creds = _load_token(SHEETS_RW_TOKEN)
    svc = build("sheets", "v4", credentials=creds)
    values = [[r[c] for c in SHEET_COLUMNS] for r in rows]
    svc.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    logger.info(f"Appended {len(values)} row(s) to roster sheet")


def build_chat_text(gaps: list, mismatches: list, resigned: list = None) -> str:
    resigned = resigned or []
    lines = ["*Roster watcher*"]
    if gaps:
        lines.append("")
        lines.append(f"{len(gaps)} employee(s) submitting reports but MISSING from the HR "
                     f"roster — please add them to the roster sheet so they sync:")
        for r in gaps:
            known = ", ".join(filter(None, [
                r["email"] or None,
                r["carrier_group"] or None,
                f"hired {r['hire_date']}" if r["hire_date"] else None,
            ]))
            lines.append(f"- {r['full_name']} ({r['emp_id']})" + (f" | {known}" if known else ""))
    if resigned:
        lines.append("")
        lines.append(f"{len(resigned)} employee(s) set INACTIVE (removed from the HR active roster):")
        for e in resigned:
            lines.append(f"- {e.get('full_name', '')} ({e.get('emp_id', '')})"
                         + (f" <{e.get('email')}>" if e.get('email') else ""))
    if mismatches:
        lines.append("")
        lines.append("Timer emails that match no roster email (fix the roster sheet email):")
        for m in mismatches:
            lines.append(
                f"- {m['user_name']} <{m['email']}> | {m['entries']} entries, last {m['last_entry']}"
            )
    return "\n".join(lines)


def post_chat(text: str) -> None:
    space = os.environ.get("ROSTER_WATCH_CHAT_SPACE", "").strip()
    if not space:
        logger.info("ROSTER_WATCH_CHAT_SPACE unset; skipping chat")
        return
    creds = _load_token(CHAT_TOKEN)
    chat = build("chat", "v1", credentials=creds)
    chat.spaces().messages().create(parent=space, body={"text": text}).execute()
    logger.info(f"Posted chat to {space}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Append to sheet, run sync, and post chat (default: dry run)")
    ap.add_argument("--no-chat", action="store_true", help="Skip the chat post")
    args = ap.parse_args()

    db = get_db()
    try:
        gaps = find_missing_reporters(db)
        rows = [build_sheet_row(db, g) for g in gaps]
        mismatches = find_orphan_timer_emails(db)
        # Emails the appends will absorb are not mismatches.
        absorbed = {r["email"] for r in rows if r["email"]}
        mismatches = [m for m in mismatches if m["email"] not in absorbed]

        print(f"Missing reporters (last {LOOKBACK_DAYS}d): {len(rows)}")
        for r in rows:
            print(f"  {r['emp_id']} | {r['full_name']} | {r['email'] or 'no email'}"
                  f" | {r['carrier_group'] or 'no CG'} | hired {r['hire_date'] or '?'}")
        print(f"Orphan timer emails: {len(mismatches)}")
        for m in mismatches:
            print(f"  {m['user_name']} <{m['email']}> last {m['last_entry']}")

        # HR owns the authoritative "Active Employee Information" roster now, so we
        # report gaps for HR to add (rather than writing to their sheet) and always
        # sync so their adds/edits and departures (absent from the active roster ->
        # is_active=false) flow into the DB.
        if not args.apply:
            print("\nDry run; use --apply to sync the HR roster -> DB.")
            sync(db, read_sheet(), effective_date=date.today(), apply=False)
            return

        summary = sync(db, read_sheet(), effective_date=date.today(), apply=True)
        resigned = summary.get("resigned", []) if summary else []

        if not rows and not mismatches and not resigned:
            print("No gaps, mismatches, or departures; nothing to report.")
            return

        if not args.no_chat:
            post_chat(build_chat_text(rows, mismatches, resigned))
    finally:
        close_db()


if __name__ == "__main__":
    main()
