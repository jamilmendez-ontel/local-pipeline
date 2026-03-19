#!/usr/bin/env python3
"""
Timer Entries Review System — Corrections, Removals, and Duplicate Handling

Techs receive a daily email listing their previous day's timer entries. Each
entry has two actions:
    - "Correct" — fix a wrong duration (opens Google Form with duration picker)
    - "Remove"  — remove a duplicate/wrong entry entirely

Corrections are stored in stg_timer_corrections, removals in
stg_timer_entry_removals (separate table). Both applied to
stg_timer_activities_clean via rebuild_timer_clean() — the original
stg_timer_activities is never modified.

Correction has higher priority than removal — if an entry is both removed AND
corrected, the correction wins and the entry stays with the corrected duration.

If a corrected/removed entry belongs to an unresolved duplicate group, that
group is auto-resolved (correction supersedes duplicate review).

Entry ID = 12-char md5 hash of (project_did|user_email|start_time|site_name|
site_id|task|end_time|duration_min). This uniquely identifies a single timer
row including its current end_time/duration.

Modes:
    --send    Send daily email to each tech with previous day's entries
    --apply   Read form responses (corrections + removals), rebuild clean table
    --remind  Send reminder for unresolved duplicate groups (reply to daily email)
    --test    Route all emails to jamil only

Usage:
    python timer_correction_review.py --send                # email all techs
    python timer_correction_review.py --send --test         # email jamil only
    python timer_correction_review.py --apply               # process form responses
    python timer_correction_review.py --remind --test       # send duplicate reminders
    python timer_correction_review.py --apply --send --remind --test  # all in one
"""

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote
from zoneinfo import ZoneInfo

from config import SCHEMA_STAGING, get_logger, get_db, close_db, retry_db, setup_logging

logger = get_logger("timer_correction")

TZ_EASTERN = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------
# Google Form configuration (jamil.mendez@ontel.co)
# --------------------------------------------------------------------------
# Correction form — Entry ID, Entry Details, Correct Duration, Reason
CORRECT_FORM_ID = "1FAIpQLSeOEgGOctEgvs1tQ5BZzH2YF2XA_s5oIeoBH_3C1fVWxl4cmQ"
CORRECT_FORM_ENTRY_ID = "entry.396920564"
CORRECT_FORM_ENTRY_DETAILS = "entry.536125253"
CORRECT_FORM_ENTRY_DURATION = "entry.348335128"
CORRECT_FORM_ENTRY_REASON = "entry.1022102307"
CORRECT_RESPONSE_SHEET_ID = "1iOGEOpD5cq-rHUKEaQQigaRFNNPLGNeMzA4VzLom7xk"

# Remove form — Entry ID, Entry Details (just submit to confirm)
REMOVE_FORM_ID = "1FAIpQLSdI4f3w3eQ2nm_WewsMggsaxYrGCLkvipOKF31S1ZcH_xGfPA"
REMOVE_FORM_ENTRY_ID = "entry.1674974379"
REMOVE_FORM_ENTRY_DETAILS = "entry.2098834655"
REMOVE_RESPONSE_SHEET_ID = "1rJm-5hr3xiwv_BZgcZRP2a8r1voZrJauj8hd2NX3L8E"

# Auto-resolve duplicate groups after this many days with no action
AUTO_RESOLVE_DAYS = 7


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _pg_ts(dt: datetime | None) -> str:
    """Format a datetime the same way PostgreSQL's ::text cast does.

    PostgreSQL timestamptz::text -> '2026-03-17 14:30:00+00'
    We replicate this so Python and SQL md5() produce the same hash.
    """
    if dt is None or not isinstance(dt, datetime):
        return "None"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    base = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
    if dt_utc.microsecond:
        frac = f".{dt_utc.microsecond:06d}".rstrip("0")
        base += frac
    return base + "+00"


def _make_entry_id(project_did: str, user_email: str, start_time: datetime,
                   site_name: str = None, site_id: str = None, task: str = None,
                   end_time: datetime = None, duration_min=None) -> str:
    """Create a 12-char hex entry ID from the full natural key."""
    dur_str = str(duration_min) if duration_min is not None else "None"
    raw = (f"{project_did}|{user_email}|{_pg_ts(start_time)}"
           f"|{site_name}|{site_id}|{task}|{_pg_ts(end_time)}|{dur_str}")
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _make_group_id(project_did: str, user_email: str, start_time: datetime,
                   site_name: str = None, site_id: str = None, task: str = None) -> str:
    """Create a 12-char hex group ID from the duplicate key (same as timer_duplicate_review)."""
    raw = f"{project_did}|{user_email}|{start_time.isoformat()}|{site_name}|{site_id}|{task}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _fmt_time(dt) -> str:
    """Format a datetime to Eastern Time string."""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_EASTERN).strftime("%Y-%m-%d %I:%M %p")


def _fmt_time_short(dt) -> str:
    """Format a datetime to short Eastern Time (date + time)."""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_EASTERN).strftime("%m/%d %I:%M %p")


def _fmt_date(dt) -> str:
    """Format a datetime to Eastern date string."""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_EASTERN).strftime("%b %d, %Y")


def _fmt_duration(minutes) -> str:
    """Format duration in minutes to a readable string."""
    if minutes is None:
        return "-"
    minutes = float(minutes)
    if minutes < 60:
        return f"{minutes:.0f} min"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _correct_form_url(entry_id: str, details: str) -> str:
    """Build a pre-filled Correction form URL."""
    base = f"https://docs.google.com/forms/d/e/{CORRECT_FORM_ID}/viewform"
    return (f"{base}"
            f"?{CORRECT_FORM_ENTRY_ID}={quote(entry_id)}"
            f"&{CORRECT_FORM_ENTRY_DETAILS}={quote(details)}")


def _remove_form_url(entry_id: str, details: str) -> str:
    """Build a pre-filled Remove form URL."""
    base = f"https://docs.google.com/forms/d/e/{REMOVE_FORM_ID}/viewform"
    return (f"{base}"
            f"?{REMOVE_FORM_ENTRY_ID}={quote(entry_id)}"
            f"&{REMOVE_FORM_ENTRY_DETAILS}={quote(details)}")


def _parse_duration_response(value: str) -> float | None:
    """Parse duration from Google Forms response.

    Google Forms Duration field returns "01:30:00" (HH:MM:SS).
    """
    if not value or not value.strip():
        return None
    value = value.strip()

    # HH:MM:SS format (Google Forms duration picker)
    m = re.match(r'^(\d+):(\d{2}):(\d{2})$', value)
    if m:
        hours, mins, secs = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return hours * 60 + mins + secs / 60

    # HH:MM format
    m = re.match(r'^(\d+):(\d{2})$', value)
    if m:
        hours, mins = int(m.group(1)), int(m.group(2))
        return hours * 60 + mins

    # Plain number (minutes)
    try:
        return float(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# --send: Email each tech with previous day's entries
# --------------------------------------------------------------------------

def get_previous_day_entries(db) -> list[dict]:
    """Query previous day's timer entries (Eastern Time)."""
    now_et = datetime.now(TZ_EASTERN)
    yesterday = (now_et - timedelta(days=1)).date()

    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT DISTINCT project_did, project, user_email, start_time, end_time,
                   duration_min, site_name, site_id, task
            FROM {SCHEMA_STAGING}.stg_timer_activities
            WHERE DATE(start_time AT TIME ZONE 'America/New_York') = $1
            ORDER BY user_email, site_name, task, start_time
        """, yesterday),
        description="fetch previous day timer entries",
    )

    return [dict(r) for r in rows] if rows else []


def _build_entries_html(entries: list[dict]) -> str:
    """Build HTML table rows for a tech's timer entries.

    Two levels of highlighting:
    - Same site + task group (2+ entries) → matching background color
    - Actual duplicates (same start_time too) → matching background color + DUPLICATE badge
    """
    # Pastel highlight colors for groups (up to 8 distinct groups)
    GROUP_COLORS = ["#FFF3E0", "#E3F2FD", "#F3E5F5", "#E8F5E9", "#FFF9C4", "#FCE4EC", "#E0F7FA", "#FBE9E7"]

    # Group by site + task (for background color)
    site_task_map = {}  # key -> list of indices
    for i, entry in enumerate(entries):
        key = (entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        site_task_map.setdefault(key, []).append(i)

    # Assign colors to site+task groups with 2+ entries
    row_color = {}
    color_idx = 0
    for key, indices in site_task_map.items():
        if len(indices) >= 2:
            color = GROUP_COLORS[color_idx % len(GROUP_COLORS)]
            for idx in indices:
                row_color[idx] = color
            color_idx += 1

    # Detect actual duplicates (same start_time) for DUPLICATE badge
    dup_key_map = {}  # key -> list of indices
    for i, entry in enumerate(entries):
        key = (entry["project_did"], entry["user_email"], entry["start_time"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        dup_key_map.setdefault(key, []).append(i)

    is_duplicate = set()
    for key, indices in dup_key_map.items():
        if len(indices) >= 2:
            is_duplicate.update(indices)

    rows_html = []
    for i, entry in enumerate(entries):
        entry_id = _make_entry_id(
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
            entry.get("end_time"), entry.get("duration_min"),
        )
        project = entry.get("project") or "(no project)"
        site = entry.get("site_name") or "(no site)"
        task = entry.get("task") or "(no task)"
        date = _fmt_date(entry["start_time"])
        start = _fmt_time_short(entry["start_time"])
        end = _fmt_time_short(entry.get("end_time"))
        duration = _fmt_duration(entry.get("duration_min"))

        # Details string for pre-fill (visible context in form)
        details = f"{project} | {site} | {task} | {date} | {duration}"
        correct_link = _correct_form_url(entry_id, details)
        remove_link = _remove_form_url(entry_id, details)

        bg = row_color.get(i, "")
        row_style = f"background:{bg};" if bg else ""
        dup_badge = (' <span style="display:inline-block;background:#e65100;color:white;'
                     'font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;'
                     'margin-left:4px;">DUPLICATE</span>') if i in is_duplicate else ""

        rows_html.append(f"""
            <tr style="{row_style}">
                <td style="padding:6px 10px;border:1px solid #ddd;">{date}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{project}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{site}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{task}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{start}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{end}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{duration}{dup_badge}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;text-align:center;white-space:nowrap;">
                    <a href="{correct_link}" style="display:inline-block;padding:5px 12px;
                       background:#1565c0;color:white;text-decoration:none;
                       border-radius:4px;font-size:11px;font-weight:bold;">Edit</a>
                    <a href="{remove_link}" style="display:inline-block;padding:5px 12px;
                       background:#c62828;color:white;text-decoration:none;
                       border-radius:4px;font-size:11px;font-weight:bold;margin-left:4px;">Remove</a>
                </td>
            </tr>""")

    return "\n".join(rows_html)


def send_daily_emails(db, entries: list[dict], test_mode: bool = False):
    """Send one email per tech with their previous day's entries.

    Stores thread_id + message_id in stg_timer_daily_notifications for
    reminder threading.
    """
    from gmail_client import authenticate

    by_user = {}
    for e in entries:
        by_user.setdefault(e["user_email"], []).append(e)

    now_et = datetime.now(TZ_EASTERN)
    yesterday = (now_et - timedelta(days=1)).date()
    date_str = yesterday.strftime("%B %d, %Y")

    service = authenticate()
    sent = 0

    for user_email, user_entries in by_user.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(user_entries)

        table_rows = _build_entries_html(user_entries)

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#1565c0;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Activity Entries - {date_str}</h2>
            </div>
            <div style="padding:24px;">
                <p>Hi,</p>
                <p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
                   from <strong>{date_str}</strong>.</p>
                <ul style="font-size:13px;color:#555;margin:8px 0 16px;">
                    <li><strong style="color:#1565c0;">Edit</strong> — fix a wrong duration</li>
                    <li><strong style="color:#c62828;">Remove</strong> — delete a duplicate or incorrect entry</li>
                </ul>

                <table style="border-collapse:collapse;width:100%;font-size:13px;margin:16px 0;">
                    <thead>
                        <tr style="background:#f5f5f5;">
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Date</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Project</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Site</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Task</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Start</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">End</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Duration</th>
                            <th style="padding:8px 10px;border:1px solid #ddd;text-align:center;">Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>

                <p style="color:#888;font-size:12px;margin-top:24px;">
                    Only click a button if something needs to be changed.
                    Links remain valid &mdash; you can correct or remove entries from older emails too.
                </p>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        msg["Subject"] = f"Timer Activity Entries - {date_str}"
        msg.attach(MIMEText(html_body, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            sent += 1

            # Store thread_id + message_id for reminder threading
            thread_id = result.get("threadId")
            gmail_msg_id = result.get("id")
            message_id = None
            if gmail_msg_id:
                sent_msg = service.users().messages().get(
                    userId="me", id=gmail_msg_id, format="metadata",
                    metadataHeaders=["Message-ID"]
                ).execute()
                for header in sent_msg.get("payload", {}).get("headers", []):
                    if header["name"].lower() == "message-id":
                        message_id = header["value"]
                        break

            if thread_id:
                retry_db(
                    lambda ue=user_email, sd=yesterday, tid=thread_id, mid=message_id: db.execute(
                        f"""INSERT INTO {SCHEMA_STAGING}.stg_timer_daily_notifications
                            (user_email, send_date, thread_id, message_id)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (user_email, send_date) DO UPDATE SET
                                thread_id = EXCLUDED.thread_id,
                                message_id = EXCLUDED.message_id
                        """,
                        ue, sd, tid, mid,
                    ),
                    description=f"store notification thread for {user_email}",
                )

            logger.info(f"Sent daily entries email to {recipient} ({n} entries, thread={thread_id})")
        except Exception as e:
            logger.error(f"Failed to send email to {recipient}: {e}")

    logger.info(f"Send complete: {sent} emails sent to {len(by_user)} techs "
                f"({sum(len(v) for v in by_user.values())} total entries)")


def detect_and_track_duplicates(db, entries: list[dict]):
    """Detect duplicate entries and create/update review records.

    Duplicates share (project_did, user_email, start_time, site_name, site_id, task)
    but differ in end_time/duration. Uses the same stg_timer_duplicate_reviews table
    as the legacy duplicate review system.
    """
    import string
    LABELS = list(string.ascii_uppercase)

    # Group entries by duplicate key
    groups = {}
    for e in entries:
        key = (e["project_did"], e["user_email"], e["start_time"],
               e.get("site_name"), e.get("site_id"), e.get("task"))
        groups.setdefault(key, []).append(e)

    # Filter to groups with 2+ entries (actual duplicates)
    dup_groups = []
    for (project_did, user_email, start_time, site_name, site_id, task), raw_entries in groups.items():
        # Filter out NULL end_time (still-running timers)
        completed = [r for r in raw_entries if r.get("end_time") is not None]
        if len(completed) < 2:
            continue

        sorted_entries = sorted(completed, key=lambda r: float(r.get("duration_min") or 0))
        group_entries = []
        for i, e in enumerate(sorted_entries):
            if i >= len(LABELS):
                break
            group_entries.append({
                "label": LABELS[i],
                "end_time": e["end_time"],
                "duration_min": e.get("duration_min"),
            })

        group_id = _make_group_id(project_did, user_email, start_time, site_name, site_id, task)
        dup_groups.append({
            "group_id": group_id,
            "project_did": project_did,
            "project": completed[0].get("project"),
            "user_email": user_email,
            "start_time": start_time,
            "site_name": site_name,
            "site_id": site_id,
            "task": task,
            "entries": group_entries,
        })

    if not dup_groups:
        return

    # Check which groups are already tracked
    group_ids = [g["group_id"] for g in dup_groups]
    existing = retry_db(
        lambda: db.fetch(
            f"SELECT group_id FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews WHERE group_id = ANY($1)",
            group_ids,
        ),
        description="check existing duplicate groups",
    )
    existing_ids = {row["group_id"] for row in existing}

    new_groups = [g for g in dup_groups if g["group_id"] not in existing_ids]
    if not new_groups:
        return

    # Insert new duplicate review records
    now = datetime.now(timezone.utc)
    from timer_duplicate_review import _entries_to_jsonb

    for g in new_groups:
        entries_json = _entries_to_jsonb(g["entries"])
        retry_db(
            lambda g=g, ej=entries_json: db.execute(
                f"""INSERT INTO {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                    (group_id, project_did, project, user_email, start_time,
                     site_name, site_id, task, entries, status, notified_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                g["group_id"], g["project_did"], g["project"], g["user_email"],
                g["start_time"], g["site_name"], g["site_id"], g["task"],
                ej, "notified", now,
            ),
            description=f"insert duplicate review {g['group_id']}",
        )

    logger.info(f"Tracked {len(new_groups)} new duplicate groups from daily entries")


def run_send(test_mode: bool = False):
    """Send daily timer entry emails and track duplicates."""
    if "PLACEHOLDER" in CORRECT_FORM_ID or "PLACEHOLDER" in REMOVE_FORM_ID:
        logger.warning("Google Form ID is still a placeholder — emails will have broken links.")

    db = get_db()

    logger.info("Fetching previous day's timer entries...")
    entries = get_previous_day_entries(db)

    if not entries:
        logger.info("No timer entries found for previous day")
        return

    n_techs = len(set(e['user_email'] for e in entries))
    logger.info(f"Found {len(entries)} entries for {n_techs} techs")

    send_daily_emails(db, entries, test_mode=test_mode)
    detect_and_track_duplicates(db, entries)


# --------------------------------------------------------------------------
# --apply: Read form responses, store corrections/removals, rebuild
# --------------------------------------------------------------------------

def read_form_responses() -> list[dict]:
    """Read corrections and removals from their separate Google Sheets."""
    from sheets_client import authenticate_sheets, read_spreadsheet

    creds = authenticate_sheets()
    by_entry = {}  # Dedup by entry_id — last response wins

    # --- Correction responses ---
    logger.info("Reading correction form responses...")
    corr_rows = read_spreadsheet(creds, CORRECT_RESPONSE_SHEET_ID)
    if len(corr_rows) > 1:
        headers = [h.strip().lower() for h in corr_rows[0]]
        for row in corr_rows[1:]:
            row_dict = {}
            for i, val in enumerate(row):
                if i < len(headers):
                    row_dict[headers[i]] = val.strip()

            entry_id = row_dict.get("entry id", "").strip()
            if not entry_id:
                continue

            duration_raw = row_dict.get("correct duration", "").strip()
            reason = row_dict.get("reason", "").strip()

            duration_min = _parse_duration_response(duration_raw)
            if duration_min is None:
                logger.warning(f"Could not parse duration '{duration_raw}' for entry {entry_id}, skipping")
                continue

            by_entry[entry_id] = {
                "entry_id": entry_id,
                "action": "correct",
                "corrected_duration_min": duration_min,
                "reason": reason or None,
            }

    # --- Removal responses ---
    logger.info("Reading removal form responses...")
    rem_rows = read_spreadsheet(creds, REMOVE_RESPONSE_SHEET_ID)
    if len(rem_rows) > 1:
        headers = [h.strip().lower() for h in rem_rows[0]]
        for row in rem_rows[1:]:
            row_dict = {}
            for i, val in enumerate(row):
                if i < len(headers):
                    row_dict[headers[i]] = val.strip()

            entry_id = row_dict.get("entry id", "").strip()
            if not entry_id:
                continue

            # Correction overrides removal — if same entry_id has a correction, skip the removal
            if entry_id in by_entry and by_entry[entry_id]["action"] == "correct":
                logger.info(f"Entry {entry_id} has both correction and removal — correction wins, skipping removal")
                continue

            by_entry[entry_id] = {
                "entry_id": entry_id,
                "action": "remove",
                "corrected_duration_min": None,
                "reason": None,
            }

    results = list(by_entry.values())
    corrections = sum(1 for r in results if r["action"] == "correct")
    removals = sum(1 for r in results if r["action"] == "remove")
    logger.info(f"Parsed {len(results)} responses (deduped): {corrections} corrections, {removals} removals")
    return results


def lookup_entry_by_id(db, entry_id: str) -> dict | None:
    """Find the timer entry matching the given entry_id hash."""
    # Check if we already have this entry stored in corrections or removals
    existing = retry_db(
        lambda: db.fetchrow(
            f"SELECT * FROM {SCHEMA_STAGING}.stg_timer_corrections WHERE entry_id = $1",
            entry_id,
        ),
        description=f"lookup existing correction {entry_id}",
    )
    if existing:
        return dict(existing)

    existing_rm = retry_db(
        lambda: db.fetchrow(
            f"SELECT * FROM {SCHEMA_STAGING}.stg_timer_entry_removals WHERE entry_id = $1",
            entry_id,
        ),
        description=f"lookup existing removal {entry_id}",
    )
    if existing_rm:
        return dict(existing_rm)

    # Recompute hash in SQL to match Python's _make_entry_id()
    row = retry_db(
        lambda: db.fetchrow(f"""
            SELECT project_did, project, user_email, start_time, site_name,
                   site_id, task, end_time, duration_min
            FROM {SCHEMA_STAGING}.stg_timer_activities
            WHERE LEFT(MD5(
                project_did || '|' || user_email || '|' ||
                (start_time AT TIME ZONE 'UTC')::text || '+00' || '|' ||
                COALESCE(site_name, 'None') || '|' ||
                COALESCE(site_id, 'None') || '|' ||
                COALESCE(task, 'None') || '|' ||
                COALESCE((end_time AT TIME ZONE 'UTC')::text || '+00', 'None') || '|' ||
                COALESCE(duration_min::text, 'None')
            ), 12) = $1
            LIMIT 1
        """, entry_id),
        description=f"lookup entry by hash {entry_id}",
    )

    return dict(row) if row else None


def _resolve_duplicate_for_action(db, entry: dict, action: str, now: datetime):
    """If the entry belongs to an unresolved duplicate group, auto-resolve it.

    For corrections: corrected entry is kept, others rejected.
    For removals: removed entry is rejected, others kept (if only 2 in group,
    the remaining one is the selected entry).
    """
    review = retry_db(
        lambda: db.fetchrow(
            f"""SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                WHERE status IN ('pending', 'notified')
                  AND project_did = $1
                  AND user_email = $2
                  AND start_time = $3
                  AND site_name IS NOT DISTINCT FROM $4
                  AND site_id IS NOT DISTINCT FROM $5
                  AND task IS NOT DISTINCT FROM $6
            """,
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
        ),
        description="check duplicate group for action",
    )

    if not review:
        return

    group_id = review["group_id"]
    entries = review["entries"] if isinstance(review["entries"], list) else json.loads(review["entries"])

    # Find which label matches this entry (by end_time + duration_min)
    entry_end = entry.get("end_time")
    entry_dur = entry.get("duration_min")
    matched_label = None

    for e in entries:
        e_end = e.get("end_time")
        e_dur = e.get("duration_min")

        # Compare end_time
        if e_end is not None and entry_end is not None:
            if isinstance(e_end, str):
                try:
                    e_end_dt = datetime.fromisoformat(e_end)
                except (ValueError, TypeError):
                    e_end_dt = None
            else:
                e_end_dt = e_end
            if entry_end.tzinfo is None:
                entry_end_cmp = entry_end.replace(tzinfo=timezone.utc)
            else:
                entry_end_cmp = entry_end
            if e_end_dt and e_end_dt.tzinfo is None:
                e_end_dt = e_end_dt.replace(tzinfo=timezone.utc)
            end_match = e_end_dt == entry_end_cmp if e_end_dt else False
        elif e_end is None and entry_end is None:
            end_match = True
        else:
            end_match = False

        # Compare duration_min
        if e_dur is not None and entry_dur is not None:
            dur_match = float(e_dur) == float(entry_dur)
        elif e_dur is None and entry_dur is None:
            dur_match = True
        else:
            dur_match = False

        if end_match and dur_match:
            matched_label = e["label"]
            break

    if not matched_label:
        logger.warning(f"Entry matches duplicate group {group_id} but couldn't match a label — skipping auto-resolve")
        return

    if action == "remove":
        # Remove = reject this entry, keep the others
        # If only 2 entries, the other one becomes selected
        remaining = [e for e in entries if e["label"] != matched_label]
        if len(remaining) == 1:
            selected_label = remaining[0]["label"]
        else:
            # Multiple remaining — keep latest end_time
            best = max(remaining, key=lambda e: e.get("end_time") or "")
            selected_label = best["label"]
        rejected = [{"end_time": e.get("end_time"), "duration_min": e.get("duration_min")}
                     for e in entries if e["label"] != selected_label]
    else:
        # Correct = keep this entry, reject others
        selected_label = matched_label
        rejected = [{"end_time": e.get("end_time"), "duration_min": e.get("duration_min")}
                     for e in entries if e["label"] != selected_label]

    retry_db(
        lambda gid=group_id, sel=selected_label, rej=rejected: db.execute(
            f"""UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                SET status = 'resolved', selected_entry = $1,
                    rejected_entries = $2,
                    resolved_at = $3, resolved_by = 'correction', updated_at = $3
                WHERE group_id = $4
            """,
            sel, rej, now, gid,
        ),
        description=f"auto-resolve duplicate {group_id} via {action}",
    )
    logger.info(f"Auto-resolved duplicate group {group_id} via {action}: kept {selected_label}, "
                f"rejected {len(rejected)} others")


def apply_responses(db, responses: list[dict]):
    """Store corrections in stg_timer_corrections, removals in stg_timer_entry_removals.

    Correction overrides removal — if the same entry is later corrected, the
    removal row stays but rebuild_timer_clean() keeps the entry (correction wins).
    """
    now = datetime.now(timezone.utc)
    applied = 0

    for resp in responses:
        entry_id = resp["entry_id"]
        action = resp["action"]
        corrected_duration = resp.get("corrected_duration_min")
        reason = resp["reason"]

        entry = lookup_entry_by_id(db, entry_id)
        if not entry:
            logger.warning(f"No timer entry found for entry_id={entry_id}, skipping")
            continue

        start_time = entry["start_time"]
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        if action == "correct":
            corrected_end_time = start_time + timedelta(minutes=corrected_duration)

            # Upsert into stg_timer_corrections (entry_id is UNIQUE — last wins)
            retry_db(
                lambda eid=entry_id, e=entry, cd=corrected_duration, cet=corrected_end_time, r=reason: db.execute(
                    f"""INSERT INTO {SCHEMA_STAGING}.stg_timer_corrections
                        (entry_id, project_did, project, user_email, start_time,
                         site_name, site_id, task, end_time, original_duration_min,
                         corrected_duration_min, corrected_end_time, reason, status,
                         corrected_at, created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'corrected',$14,$14,$14)
                        ON CONFLICT (entry_id) DO UPDATE SET
                            corrected_duration_min = EXCLUDED.corrected_duration_min,
                            corrected_end_time = EXCLUDED.corrected_end_time,
                            reason = EXCLUDED.reason,
                            corrected_at = EXCLUDED.corrected_at,
                            updated_at = EXCLUDED.updated_at
                    """,
                    eid, e["project_did"], e.get("project"), e["user_email"],
                    e["start_time"], e.get("site_name"), e.get("site_id"),
                    e.get("task"), e.get("end_time"), e.get("duration_min"),
                    cd, cet, r, now,
                ),
                description=f"upsert correction {entry_id}",
            )
            logger.info(f"Stored correction {entry_id}: "
                        f"{_fmt_duration(entry.get('duration_min'))} -> {_fmt_duration(corrected_duration)} "
                        f"(reason: {reason or 'none'})")

        else:
            # Upsert into stg_timer_entry_removals (entry_id is UNIQUE — last wins)
            retry_db(
                lambda eid=entry_id, e=entry, r=reason: db.execute(
                    f"""INSERT INTO {SCHEMA_STAGING}.stg_timer_entry_removals
                        (entry_id, project_did, project, user_email, start_time,
                         site_name, site_id, task, end_time, duration_min,
                         reason, removed_at, created_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12,$12)
                        ON CONFLICT (entry_id) DO UPDATE SET
                            reason = EXCLUDED.reason,
                            removed_at = EXCLUDED.removed_at,
                            updated_at = EXCLUDED.updated_at
                    """,
                    eid, e["project_did"], e.get("project"), e["user_email"],
                    e["start_time"], e.get("site_name"), e.get("site_id"),
                    e.get("task"), e.get("end_time"), e.get("duration_min"),
                    r, now,
                ),
                description=f"upsert removal {entry_id}",
            )
            logger.info(f"Stored removal {entry_id}: "
                        f"{entry.get('site_name') or '(no site)'} / {entry.get('task') or '(no task)'} "
                        f"(reason: {reason or 'none'})")

        applied += 1

        # Auto-resolve any related duplicate group
        _resolve_duplicate_for_action(db, entry, action, now)

    if applied:
        logger.info(f"Applied {applied} responses, rebuilding clean table...")
        rebuild_clean_table(db)
    else:
        logger.info("No new responses to apply")


def rebuild_clean_table(db):
    """Rebuild stg_timer_activities_clean via the database RPC."""
    logger.info("Rebuilding stg_timer_activities_clean...")
    retry_db(
        lambda: db.execute(f"SELECT {SCHEMA_STAGING}.rebuild_timer_clean()"),
        description="rebuild_timer_clean",
    )
    count = retry_db(
        lambda: db.fetchval(f"SELECT COUNT(*) FROM {SCHEMA_STAGING}.stg_timer_activities_clean"),
        description="count clean table",
    )
    logger.info(f"Clean table rebuilt: {count:,} rows")


def auto_resolve_stale(db):
    """Auto-resolve duplicate groups older than AUTO_RESOLVE_DAYS: keep latest end_time."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=AUTO_RESOLVE_DAYS)

    stale = retry_db(
        lambda: db.fetch(
            f"""SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                WHERE status IN ('pending', 'notified')
                  AND notified_at IS NOT NULL AND notified_at < $1
            """, cutoff,
        ),
        description="find stale duplicate reviews",
    )

    if not stale:
        logger.info("No stale duplicate reviews to auto-resolve")
        return False

    logger.info(f"Auto-resolving {len(stale)} stale duplicate groups (>{AUTO_RESOLVE_DAYS} days)...")

    for review in stale:
        entries = review["entries"] if isinstance(review["entries"], list) else json.loads(review["entries"])
        best = max(entries, key=lambda e: e.get("end_time") or "")
        selection = best["label"]
        rejected = [{"end_time": e.get("end_time"), "duration_min": e.get("duration_min")}
                     for e in entries if e["label"] != selection]

        retry_db(
            lambda gid=review["group_id"], sel=selection, rej=rejected: db.execute(
                f"""UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                    SET status = 'auto_resolved', selected_entry = $1,
                        rejected_entries = $2,
                        resolved_at = $3, resolved_by = 'auto', updated_at = $3
                    WHERE group_id = $4
                """,
                sel, rej, now, gid,
            ),
            description=f"auto-resolve {review['group_id']}",
        )
        logger.info(f"Auto-resolved group {review['group_id']}: kept {selection}")

    return True


def run_apply():
    """Process form responses, auto-resolve stale duplicates, rebuild clean table."""
    db = get_db()

    # 1. Process form responses
    responses = read_form_responses()
    if responses:
        apply_responses(db, responses)

    # 2. Auto-resolve stale duplicate groups
    auto_resolved = auto_resolve_stale(db)

    # 3. Always rebuild (picks up new staging data + any auto-resolves)
    if not responses:
        rebuild_clean_table(db)
    elif auto_resolved:
        rebuild_clean_table(db)


# --------------------------------------------------------------------------
# --remind: Send reminders for unresolved duplicate groups
# --------------------------------------------------------------------------

def run_remind(test_mode: bool = False):
    """Send reminder emails for unresolved duplicate groups.

    Replies to the original daily entries email so the tech sees it in the
    same thread.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    unresolved = retry_db(
        lambda: db.fetch(
            f"""SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                WHERE status IN ('pending', 'notified')
                  AND notified_at IS NOT NULL
            """,
        ),
        description="find unresolved duplicate reviews",
    )

    if not unresolved:
        logger.info("No unresolved duplicate reviews need reminders")
        return

    from gmail_client import authenticate

    # Group by user_email
    by_user = {}
    for r in unresolved:
        entries = r["entries"] if isinstance(r["entries"], list) else json.loads(r["entries"])
        days_pending = (now - r["notified_at"]).days
        by_user.setdefault(r["user_email"], []).append({
            "group_id": r["group_id"],
            "project": r["project"],
            "site_name": r["site_name"],
            "task": r["task"],
            "start_time": r["start_time"],
            "entries": entries,
            "days_pending": days_pending,
        })

    service = authenticate()

    for user_email, user_groups in by_user.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(user_groups)
        max_days = max(g["days_pending"] for g in user_groups)
        days_left = max(0, AUTO_RESOLVE_DAYS - max_days)

        # Find the latest daily email thread for this user (for reply threading)
        notif = retry_db(
            lambda ue=user_email: db.fetchrow(
                f"""SELECT thread_id, message_id FROM {SCHEMA_STAGING}.stg_timer_daily_notifications
                    WHERE user_email = $1
                    ORDER BY send_date DESC LIMIT 1
                """, ue,
            ),
            description=f"lookup notification thread for {user_email}",
        )

        # Build summary list of duplicate groups needing action
        summary_items = []
        for g in user_groups:
            site = g.get("site_name") or "(no site)"
            task = g.get("task") or "(no task)"
            days = g["days_pending"]
            n_entries = len(g["entries"])
            summary_items.append(
                f'<li><strong>{g["project"]}</strong> &mdash; {site} &mdash; {task} '
                f'({n_entries} entries, '
                f'<span style="color:#c62828;">{days} day{"s" if days != 1 else ""} pending</span>)</li>'
            )
        summary_html = "\n".join(summary_items)

        auto_resolve_warning = (
            f"Duplicate entries will be auto-resolved in <strong>{days_left} day{'s' if days_left != 1 else ''}</strong> "
            f"(latest end time kept)."
            if days_left > 0
            else "Duplicate entries will be <strong>auto-resolved today</strong> (latest end time kept)."
        )

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#e65100;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Entries - Duplicate Reminder ({max_days} day{'s' if max_days != 1 else ''} pending)</h2>
            </div>
            <div style="padding:24px;">
                <p>Hi,</p>
                <p>You have <strong>{n}</strong> duplicate timer
                   {'group' if n == 1 else 'groups'} that still need attention.
                   Please go back to the original timer entries email and click
                   <strong style="color:#c62828;">Remove</strong> on the incorrect entries.</p>

                <p><strong>Pending duplicates:</strong></p>
                <ul style="font-size:14px;line-height:1.8;">
                    {summary_html}
                </ul>

                <p style="color:#c62828;font-size:13px;margin-top:24px;font-weight:bold;">
                    {auto_resolve_warning}
                </p>
            </div>
        </body>
        </html>
        """

        subject = (f"Re: Timer Activity Entries - Duplicate Reminder "
                   f"({max_days} day{'s' if max_days != 1 else ''} pending)")

        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        msg["Subject"] = subject
        if notif and notif.get("message_id"):
            msg["In-Reply-To"] = notif["message_id"]
            msg["References"] = notif["message_id"]
        msg.attach(MIMEText(html_body, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
            send_body = {"raw": raw}
            if notif and notif.get("thread_id"):
                send_body["threadId"] = notif["thread_id"]
            service.users().messages().send(userId="me", body=send_body).execute()
            logger.info(f"Sent duplicate reminder to {recipient} ({n} groups, "
                        f"thread={notif.get('thread_id') if notif else 'none'})")
        except Exception as e:
            logger.error(f"Failed to send reminder to {user_email}: {e}")

    # Update reminder counts
    group_ids = [g["group_id"] for ug in by_user.values() for g in ug]
    retry_db(
        lambda: db.execute(
            f"""UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews
                SET reminder_count = reminder_count + 1, last_reminder_at = $1, updated_at = $1
                WHERE group_id = ANY($2)
            """, now, group_ids,
        ),
        description="update reminder counts",
    )
    total_groups = sum(len(v) for v in by_user.values())
    logger.info(f"Remind complete: sent reminders for {total_groups} groups to {len(by_user)} techs")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Timer Entries Review System")
    parser.add_argument("--send", action="store_true", help="Send daily timer entry emails")
    parser.add_argument("--apply", action="store_true", help="Process form responses + auto-resolve stale")
    parser.add_argument("--remind", action="store_true", help="Send duplicate reminder emails")
    parser.add_argument("--test", action="store_true", help="Test mode: send all emails to jamil only")
    args = parser.parse_args()

    if not any([args.send, args.apply, args.remind]):
        parser.error("At least one of --send, --apply, --remind is required")

    try:
        if args.apply:
            logger.info("=== Running --apply ===")
            run_apply()

        if args.send:
            logger.info("=== Running --send ===")
            run_send(test_mode=args.test)

        if args.remind:
            logger.info("=== Running --remind ===")
            run_remind(test_mode=args.test)

    finally:
        close_db()


if __name__ == "__main__":
    main()
