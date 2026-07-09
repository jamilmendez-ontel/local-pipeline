#!/usr/bin/env python3
"""
Timer Entries Review System — Corrections, Removals, and Duplicate Handling

Techs receive a daily email listing their previous day's timer entries. Each
entry has two actions:
    - "Correct" — fix a wrong duration (opens Google Form with duration picker)
    - "Remove"  — remove a duplicate/wrong entry entirely

Corrections are stored in app_timer.corrections, removals in
app_timer.entry_removals (separate table). Both applied to
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

from config import SCHEMA_STAGING, SCHEMA_TIMER, get_logger, get_db, close_db, retry_db, setup_logging

logger = get_logger("timer_correction")

TZ_EASTERN = ZoneInfo("America/New_York")


def _entries_to_jsonb(entries):
    """Prepare entries list for asyncpg JSONB column.

    Ensures all values are JSON-serializable (no datetime objects).
    asyncpg's JSONB codec handles json.dumps internally.
    """
    serializable = []
    for e in entries:
        item = {}
        for k, v in e.items():
            if isinstance(v, datetime):
                item[k] = v.isoformat()
            elif v is None:
                item[k] = None
            else:
                item[k] = str(v) if not isinstance(v, str) else v
        serializable.append(item)
    return serializable


def _first_name(email):
    """Extract first name from email for greeting.

    jamil.mendez@ontel.co -> Jamil
    james@ontel.co -> James
    jm.chan@ontel.co -> Jm
    """
    if not email:
        return "there"
    local = email.split("@")[0] if "@" in email else email
    first = local.split(".")[0] if "." in local else local
    return first.capitalize() if first else "there"

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
# OAuth Token Health Check
# --------------------------------------------------------------------------

def check_token_health():
    """Verify OAuth refresh tokens are still valid. Alert via email if any fail.

    Catches the 'invalid_grant' error that occurs when a refresh token is
    revoked (e.g. GCP project reverted to Testing mode). Sends alert to
    jamil so the token can be manually re-authenticated before the pipeline
    starts silently failing.
    """
    import pickle
    from pathlib import Path
    from google.auth.transport.requests import Request

    TOKEN_DIR = Path(__file__).parent / "gmail_credentials"
    # Only check tokens used by this script (sheets for --apply)
    # Calendar token is only used by calendar_client.py, not deployed in GHA
    tokens = {
        "sheets_token.pickle": "Google Sheets (Drive API) — used by --apply",
    }

    failed = []
    for filename, description in tokens.items():
        token_path = TOKEN_DIR / filename
        if not token_path.exists():
            failed.append((filename, description, "File not found"))
            continue
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif not creds.valid and not creds.refresh_token:
                failed.append((filename, description, "No refresh token"))
                continue
        except Exception as e:
            failed.append((filename, description, str(e)))

    if not failed:
        logger.info("Token health check passed — all refresh tokens valid")
        return True

    # Build alert email
    for filename, description, error in failed:
        logger.error(f"TOKEN HEALTH CHECK FAILED: {filename} — {error}")

    try:
        from gmail_client import authenticate
        service = authenticate()

        items = "\n".join(
            f"<li><strong>{fn}</strong> ({desc})<br>"
            f"<span style='color:#c62828;'>{err}</span></li>"
            for fn, desc, err in failed
        )
        html = f"""
        <html><body style="font-family:Arial,sans-serif;">
            <div style="background:#c62828;color:white;padding:16px 24px;">
                <h2 style="margin:0;">OAuth Token Alert</h2>
            </div>
            <div style="padding:24px;">
                <p>The following OAuth tokens failed their refresh health check:</p>
                <ul>{items}</ul>
                <p>This likely means the GCP project (<code>prefab-reducer-487104-r0</code>)
                   has reverted to <strong>Testing</strong> mode, which expires refresh tokens
                   after 7 days.</p>
                <p><strong>To fix:</strong></p>
                <ol>
                    <li>Google Cloud Console → project <code>prefab-reducer-487104-r0</code>
                        (nanoninth account)</li>
                    <li>Google Auth platform → Audience → Publish App</li>
                    <li>Delete expired token locally, re-authenticate, update GHA secret</li>
                </ol>
            </div>
        </body></html>
        """
        msg = MIMEMultipart()
        msg["To"] = "jamil.mendez@ontel.co"
        msg["From"] = "me"
        msg["Subject"] = "Pipeline Alert: OAuth Token Refresh Failed"
        msg.attach(MIMEText(html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info("Token health alert email sent to jamil.mendez@ontel.co")
    except Exception as e:
        logger.error(f"Failed to send token health alert email: {e}")

    return False


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


def _entry_date_et(dt) -> "date":
    """Return the calendar date of dt interpreted in Eastern Time."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_EASTERN).date()


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


def _compute_summary_groups(entries: list[dict]) -> list[dict]:
    """Aggregate timer entries by (task_clean, site_name, project).

    Pure function. Takes the same shape of entry dicts that
    `get_previous_day_entries()` returns. Returns one dict per distinct
    (task_clean, site_name, project) combination, with per-group totals
    and a boolean flag indicating whether the group contains any
    duplicate entries.

    Duplicate detection uses the same cluster logic as `_build_entries_html`
    and `detect_and_track_duplicates`: bucket by
    (project_did, user_email, site_name, site_id, task), then look for any
    overlap cluster of size >= 2. NULL end_time entries are excluded from
    clustering.
    """
    from collections import defaultdict

    # Display buckets group by (task_clean, site_name, project) for layout.
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for e in entries:
        task = e.get("task_clean") or e.get("task") or ""
        key = (task, e.get("site_name") or "", e.get("project") or "")
        buckets[key].append(e)

    groups = []
    for (task, site, project), rows in buckets.items():
        # Detection bucket key uses raw task + site_id (matches cluster code).
        detection_buckets: dict[tuple, list[dict]] = defaultdict(list)
        for r in rows:
            if r.get("end_time") is None:
                continue
            dkey = (
                r.get("project_did"),
                r.get("user_email"),
                r.get("site_name"),
                r.get("site_id"),
                r.get("task"),
            )
            detection_buckets[dkey].append(r)

        has_duplicates = False
        for bucket_rows in detection_buckets.values():
            if len(bucket_rows) < 2:
                continue
            for cluster in _build_overlap_clusters(bucket_rows):
                if len(cluster) >= 2:
                    has_duplicates = True
                    break
            if has_duplicates:
                break

        total = sum(float(r.get("duration_min") or 0) for r in rows)

        groups.append({
            "task": task,
            "site": site,
            "project": project,
            "entries": len(rows),
            "total_duration_min": total,
            "has_duplicates": has_duplicates,
        })

    groups.sort(key=lambda g: (g["project"], g["site"], g["task"]))
    return groups


def _build_summary_html(entries: list[dict]) -> str:
    """Render the Daily Task Summary table for one tech's entries.

    Returns an empty string when entries is empty so the caller can
    unconditionally inline the result.

    Styling mirrors the existing detail table (Arial 13px, 1px borders,
    light header background). Rows flagged as containing duplicates get
    a subtle yellow-tinted background and a red warning glyph in the
    rightmost column.
    """
    groups = _compute_summary_groups(entries)
    if not groups:
        return ""

    header_style = (
        "padding:6px 10px;border:1px solid #bbb;background:#eef3fa;"
        "text-align:left;font-size:13px;"
    )
    cell_style = "padding:6px 10px;border:1px solid #ccc;font-size:13px;"
    warn_style = cell_style + "text-align:center;"
    row_dup_bg = "background:#fffbe6;"  # subtle yellow for duplicate rows

    html = [
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;'
        'margin:8px 0 16px;">',
        "<tr>",
        f'<th style="{header_style}">Project</th>',
        f'<th style="{header_style}">Site</th>',
        f'<th style="{header_style}">Task</th>',
        f'<th style="{header_style}text-align:right;">Entries</th>',
        f'<th style="{header_style}text-align:right;">Total</th>',
        f'<th style="{header_style}text-align:center;">Duplicates</th>',
        "</tr>",
    ]

    for g in groups:
        dup_cell = (
            '<span style="color:#c62828;">&#9888;</span>'
            if g["has_duplicates"] else "&mdash;"
        )
        if g["has_duplicates"]:
            html.append(f'<tr style="{row_dup_bg}">')
        else:
            html.append("<tr>")
        html.append(f'<td style="{cell_style}">{_escape_html(g["project"])}</td>')
        html.append(f'<td style="{cell_style}">{_escape_html(g["site"])}</td>')
        html.append(f'<td style="{cell_style}">{_escape_html(g["task"])}</td>')
        html.append(f'<td style="{cell_style}text-align:right;">{g["entries"]}</td>')
        html.append(
            f'<td style="{cell_style}text-align:right;">'
            f'{_fmt_duration(g["total_duration_min"])}</td>'
        )
        html.append(f'<td style="{warn_style}">{dup_cell}</td>')
        html.append("</tr>")

    html.append("</table>")
    return "".join(html)


def _escape_html(value) -> str:
    """Minimal HTML escape for cell values."""
    if value is None:
        return ""
    s = str(value)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


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


def _intervals_overlap(a_start, a_end, b_start, b_end) -> bool:
    """True if [a_start, a_end) and [b_start, b_end) intersect, OR they share
    a start_time.

    All four arguments must be non-None timezone-aware datetimes. Touching
    endpoints (a_end == b_start, different starts) are NOT considered
    overlapping. Same start_time is ALWAYS considered overlap, even when one
    interval is degenerate (start == end) -- this preserves the legacy
    same-start duplicate guarantee for techs whose timer mis-fires register
    as 0-minute entries.
    """
    if a_start == b_start:
        return True
    return a_start < b_end and b_start < a_end


def _build_overlap_clusters(entries: list[dict]) -> list[list[dict]]:
    """Group entries into connected components by time overlap.

    Two entries belong to the same cluster if their [start_time, end_time)
    windows intersect, or if they transitively reach each other through a
    third overlapping entry.

    Requires each entry dict to have non-None datetime values at
    ``entry["start_time"]`` and ``entry["end_time"]``. Callers are responsible
    for filtering NULL end_time before calling this.

    Returns clusters in input-encounter order. Within each cluster, entries
    preserve their input order.

    Union-Find over O(n^2) pairwise overlap checks. n is small in practice
    (entries per (user, task, site) per day rarely exceeds a handful).
    """
    n = len(entries)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _intervals_overlap(
                entries[i]["start_time"], entries[i]["end_time"],
                entries[j]["start_time"], entries[j]["end_time"],
            ):
                union(i, j)

    # Bucket entries by their cluster root, preserving input order.
    bucket_by_root: dict[int, list[dict]] = {}
    root_order: list[int] = []
    for i in range(n):
        root = find(i)
        if root not in bucket_by_root:
            bucket_by_root[root] = []
            root_order.append(root)
        bucket_by_root[root].append(entries[i])

    return [bucket_by_root[r] for r in root_order]


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

def get_previous_day_entries(db, target_date=None) -> list[dict]:
    """Query timer entries for a given date (Eastern Time). Defaults to yesterday."""
    if target_date is None:
        now_et = datetime.now(TZ_EASTERN)
        target_date = (now_et - timedelta(days=1)).date()
    yesterday = target_date

    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT DISTINCT project_did, project, user_email, start_time, end_time,
                   duration_min, site_name, site_id, task, task_clean
            FROM {SCHEMA_STAGING}.stg_timer_activities
            WHERE DATE(start_time AT TIME ZONE 'America/New_York') = $1
            ORDER BY user_email, site_name, task, start_time
        """, yesterday),
        description="fetch previous day timer entries",
    )

    return [dict(r) for r in rows] if rows else []


def _has_duplicate_entries(entries: list[dict]) -> bool:
    """True if any cluster of 2+ entries exists by overlap on the same task.

    Matches the cluster logic used in `_build_entries_html` for the DUPLICATE
    badge: bucket by (project_did, user_email, site_name, site_id, task),
    then check for any overlap cluster of size >= 2. Entries with NULL
    end_time are excluded from clustering.
    """
    bucket_indices: dict[tuple, list[int]] = {}
    for i, entry in enumerate(entries):
        if entry.get("end_time") is None:
            continue
        key = (entry["project_did"], entry["user_email"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        bucket_indices.setdefault(key, []).append(i)
    for indices in bucket_indices.values():
        if len(indices) < 2:
            continue
        bucket_entries = [entries[i] for i in indices]
        for cluster in _build_overlap_clusters(bucket_entries):
            if len(cluster) >= 2:
                return True
    return False


def _build_entries_html(entries: list[dict]) -> str:
    """Build HTML table rows for a tech's timer entries.

    Two levels of highlighting:
    - Same site + task group (2+ entries) → matching background color
    - Actual duplicates (temporal overlap on the same task) → matching background color + DUPLICATE badge
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

    # Detect duplicates via temporal overlap on the same task. Uses the same
    # cluster logic as detect_and_track_duplicates so the daily email's
    # DUPLICATE badges match the review records we just wrote.
    is_duplicate: set[int] = set()
    bucket_indices: dict[tuple, list[int]] = {}
    for i, entry in enumerate(entries):
        if entry.get("end_time") is None:
            continue  # Still-running timers can't be assessed for overlap
        key = (entry["project_did"], entry["user_email"],
               entry.get("site_name"), entry.get("site_id"), entry.get("task"))
        bucket_indices.setdefault(key, []).append(i)

    for indices in bucket_indices.values():
        if len(indices) < 2:
            continue
        bucket_entries = [entries[i] for i in indices]
        for cluster in _build_overlap_clusters(bucket_entries):
            if len(cluster) < 2:
                continue
            # Map cluster members back to their indices in `entries`.
            for clustered in cluster:
                for idx in indices:
                    if entries[idx] is clustered:
                        is_duplicate.add(idx)
                        break

    rows_html = []
    for i, entry in enumerate(entries):
        entry_id = _make_entry_id(
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
            entry.get("end_time"), entry.get("duration_min"),
        )
        project_raw = entry.get("project") or "(no project)"
        site_raw = entry.get("site_name") or "(no site)"
        task_raw = entry.get("task") or "(no task)"
        project = _escape_html(project_raw)
        site = _escape_html(site_raw)
        task = _escape_html(task_raw)
        date = _escape_html(_fmt_date(entry["start_time"]))
        start = _escape_html(_fmt_time_short(entry["start_time"]))
        end = _escape_html(_fmt_time_short(entry.get("end_time")))
        duration = _escape_html(_fmt_duration(entry.get("duration_min")))

        # Form pre-fill uses raw values (URL-encoded inside the form helpers).
        details = f"{project_raw} | {site_raw} | {task_raw} | {_fmt_date(entry['start_time'])} | {_fmt_duration(entry.get('duration_min'))}"
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


def send_daily_emails(db, entries: list[dict], test_mode: bool = False,
                      target_date=None):
    """Send one email per tech with their entries for `target_date`.

    target_date defaults to yesterday (the normal nightly flow). For
    backfill sends (e.g., after an outage), pass the actual date the
    entries belong to so the email subject, "Daily Task Summary" date
    label, and the `send_date` column on app_timer.daily_notifications
    all reflect the entries' real date — not today-minus-1.

    Stores thread_id + message_id in app_timer.daily_notifications for
    reminder threading.

    Snapshot source asymmetry (intentional): the initial snapshot is
    seeded from `stg_timer_activities` (via get_previous_day_entries),
    while find_days_needing_resend compares against
    `stg_timer_activities_clean`. Clean is a subset (removals applied,
    duplicates collapsed), so a key written to the snapshot at send
    time may not appear in any future current_ids. That cannot cause
    a false-positive resend — the trigger checks
    `current_ids - snapshot_ids` (new keys only), so an over-broad
    snapshot is harmless. Keeping it this way avoids re-fetching from
    clean inside send_daily_emails just for snapshot purposes.
    """
    from gmail_client import authenticate

    by_user = {}
    for e in entries:
        by_user.setdefault(e["user_email"], []).append(e)

    if target_date is None:
        target_date = (datetime.now(TZ_EASTERN) - timedelta(days=1)).date()
    yesterday = target_date  # variable name preserved — used as the entry date below
    date_str = yesterday.strftime("%B %d, %Y")

    service = authenticate()
    sent = 0

    for user_email, user_entries in by_user.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(user_entries)
        has_duplicates = _has_duplicate_entries(user_entries)

        table_rows = _build_entries_html(user_entries)

        duplicate_notes = (
            "<li>You'll receive daily reminders until all duplicate entries are resolved.</li>"
            "<li>The <strong>duplicate icon</strong> highlights entries that overlap in time on the same task"
            " &mdash; these are likely system-generated duplicates.</li>"
            if has_duplicates else ""
        )

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#1565c0;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Activity Entries - {date_str}</h2>
            </div>
            <div style="padding:24px;">
                <p>Hi {_first_name(user_email)},</p>
                <p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
                   from <strong>{date_str}</strong>.</p>
                <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Daily Task Summary</h3>
                {_build_summary_html(user_entries)}

                <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Entry Details</h3>
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

                <div style="background:#f5f5f5;border-radius:6px;padding:14px 18px;margin-top:24px;font-size:13px;color:#555;">
                    <p style="margin:0 0 8px;font-weight:bold;color:#333;">A few things to note:</p>
                    <ul style="margin:0;padding-left:20px;line-height:1.8;">
                        {duplicate_notes}
                        <li>Entries are <strong>color-coded</strong> by site and task so you can easily
                            spot related groups.</li>
                    </ul>
                    <p style="margin:8px 0 0;color:#888;font-size:12px;">
                        Only click a button if something needs to be changed.
                        Links remain valid &mdash; you can correct or remove entries from older emails too.
                    </p>
                </div>
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
                # asyncpg's JSONB codec json.dumps the value on the way out;
                # pass the raw Python list so the result is a proper JSONB
                # array, not a JSONB string containing JSON text.
                entry_ids_snapshot = _collect_entry_ids(user_entries)
                retry_db(
                    lambda ue=user_email, sd=yesterday, tid=thread_id, mid=message_id,
                           eids=entry_ids_snapshot: db.execute(
                        f"""INSERT INTO {SCHEMA_TIMER}.daily_notifications
                            (user_email, send_date, thread_id, message_id,
                             last_sent_at, last_sent_entry_ids)
                            VALUES ($1, $2, $3, $4, NOW(), $5::jsonb)
                            ON CONFLICT (user_email, send_date) DO UPDATE SET
                                thread_id = EXCLUDED.thread_id,
                                message_id = EXCLUDED.message_id,
                                last_sent_at = EXCLUDED.last_sent_at,
                                last_sent_entry_ids = EXCLUDED.last_sent_entry_ids
                        """,
                        ue, sd, tid, mid, eids,
                    ),
                    description=f"store notification thread for {user_email}",
                )

            logger.info(f"Sent daily entries email to {recipient} ({n} entries, thread={thread_id})")
        except Exception as e:
            logger.error(f"Failed to send email to {recipient}: {e}")

    logger.info(f"Send complete: {sent} emails sent to {len(by_user)} techs "
                f"({sum(len(v) for v in by_user.values())} total entries)")


def detect_and_track_duplicates(db, entries: list[dict]):
    """Detect overlapping timer entries and create/update review records.

    Entries are duplicates if they share (project_did, user_email, site_name,
    site_id, task) AND their [start_time, end_time) windows intersect.
    Transitive overlap counts: A-B-C through B all land in one cluster even
    if A and C do not directly touch.

    NULL end_time entries (still-running timers) are filtered before clustering.

    group_id is anchored on the cluster's earliest start_time. For clusters
    where every entry shares the same start_time (today's classic case), this
    produces the same group_id as the legacy formula -- existing pending
    reviews keep their IDs and form threads.

    Cluster entries get persisted into app_timer.duplicate_reviews.entries
    as a JSONB array. Each element now includes start_time (was implicit in
    the parent column before; needed explicitly now that cluster members may
    have different start_times). rebuild_timer_clean() joins on this per-entry
    start_time.
    """
    import string
    LABELS = list(string.ascii_uppercase)

    # 1. Bucket by (project, user, site, task) -- start_time deliberately omitted.
    buckets: dict[tuple, list[dict]] = {}
    for e in entries:
        if e.get("end_time") is None:
            continue  # Skip still-running timers
        key = (
            e["project_did"],
            e["user_email"],
            e.get("site_name"),
            e.get("site_id"),
            e.get("task"),
        )
        buckets.setdefault(key, []).append(e)

    # 2. Build overlap clusters within each bucket. Cluster of >=2 is a duplicate.
    dup_groups: list[dict] = []
    for (project_did, user_email, site_name, site_id, task), bucket in buckets.items():
        if len(bucket) < 2:
            continue
        for cluster in _build_overlap_clusters(bucket):
            if len(cluster) < 2:
                continue

            # Sort cluster by duration_min asc for stable labelling (matches legacy)
            sorted_entries = sorted(cluster, key=lambda r: float(r.get("duration_min") or 0))
            group_entries = []
            for i, e in enumerate(sorted_entries):
                if i >= len(LABELS):
                    break
                group_entries.append({
                    "label": LABELS[i],
                    "start_time": e["start_time"],
                    "end_time": e["end_time"],
                    "duration_min": e.get("duration_min"),
                })

            earliest = min(e["start_time"] for e in cluster)
            group_id = _make_group_id(
                project_did, user_email, earliest, site_name, site_id, task,
            )
            dup_groups.append({
                "group_id": group_id,
                "project_did": project_did,
                "project": cluster[0].get("project"),
                "user_email": user_email,
                "start_time": earliest,  # Parent column holds the anchor; per-entry start_time lives in JSONB.
                "site_name": site_name,
                "site_id": site_id,
                "task": task,
                "entries": group_entries,
            })

    if not dup_groups:
        return

    # 3. Skip groups already tracked.
    group_ids = [g["group_id"] for g in dup_groups]
    existing = retry_db(
        lambda: db.fetch(
            f"SELECT group_id FROM {SCHEMA_TIMER}.duplicate_reviews WHERE group_id = ANY($1)",
            group_ids,
        ),
        description="check existing duplicate groups",
    )
    existing_ids = {row["group_id"] for row in existing}

    new_groups = [g for g in dup_groups if g["group_id"] not in existing_ids]
    if not new_groups:
        return

    # 4. Insert new review records.
    now = datetime.now(timezone.utc)
    for g in new_groups:
        entries_json = _entries_to_jsonb(g["entries"])
        retry_db(
            lambda g=g, ej=entries_json: db.execute(
                f"""INSERT INTO {SCHEMA_TIMER}.duplicate_reviews
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


def run_send(test_mode: bool = False, target_date=None):
    """Send daily timer entry emails and track duplicates."""
    if "PLACEHOLDER" in CORRECT_FORM_ID or "PLACEHOLDER" in REMOVE_FORM_ID:
        logger.warning("Google Form ID is still a placeholder — emails will have broken links.")

    db = get_db()

    date_label = target_date or "previous day"
    logger.info(f"Fetching timer entries for {date_label}...")
    entries = get_previous_day_entries(db, target_date=target_date)

    if not entries:
        logger.info("No timer entries found for previous day")
        return

    n_techs = len(set(e['user_email'] for e in entries))
    logger.info(f"Found {len(entries)} entries for {n_techs} techs")

    send_daily_emails(db, entries, test_mode=test_mode, target_date=target_date)
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


def lookup_entries_by_hash(db, entry_ids: list[str]) -> dict[str, dict]:
    """Batch-resolve entry_id hashes against stg_timer_activities in ONE scan.

    The hash is computed per row (no index can serve it), so each lookup is a
    full-table scan. Unmatched sheet rows are re-checked every --apply run
    forever; doing them one query per row blew past the workflow's job timeout
    once the table grew (48 rows x ~6s/scan on 2026-07-09). One scan resolves
    them all.
    """
    if not entry_ids:
        return {}
    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT * FROM (
                SELECT LEFT(MD5(
                    project_did || '|' || user_email || '|' ||
                    (start_time AT TIME ZONE 'UTC')::text || '+00' || '|' ||
                    COALESCE(site_name, 'None') || '|' ||
                    COALESCE(site_id, 'None') || '|' ||
                    COALESCE(task, 'None') || '|' ||
                    COALESCE((end_time AT TIME ZONE 'UTC')::text || '+00', 'None') || '|' ||
                    COALESCE(duration_min::text, 'None')
                ), 12) AS _entry_id_hash,
                project_did, project, user_email, start_time, site_name,
                site_id, task, end_time, duration_min
                FROM {SCHEMA_STAGING}.stg_timer_activities
            ) t
            WHERE _entry_id_hash = ANY($1::text[])
        """, entry_ids),
        description=f"batch lookup {len(entry_ids)} entries by hash",
    )
    result: dict[str, dict] = {}
    for r in rows or []:
        d = dict(r)
        h = d.pop("_entry_id_hash")
        result.setdefault(h, d)
    return result


def lookup_entry_by_id(db, entry_id: str, hash_map: dict[str, dict] | None = None) -> dict | None:
    """Find the timer entry matching the given entry_id hash.

    hash_map: optional pre-resolved {entry_id: row} from lookup_entries_by_hash();
    when provided, the per-row full-table hash scan is skipped.
    """
    # Check if we already have this entry stored in corrections or removals
    existing = retry_db(
        lambda: db.fetchrow(
            f"SELECT * FROM {SCHEMA_TIMER}.corrections WHERE entry_id = $1",
            entry_id,
        ),
        description=f"lookup existing correction {entry_id}",
    )
    if existing:
        return dict(existing)

    existing_rm = retry_db(
        lambda: db.fetchrow(
            f"SELECT * FROM {SCHEMA_TIMER}.entry_removals WHERE entry_id = $1",
            entry_id,
        ),
        description=f"lookup existing removal {entry_id}",
    )
    if existing_rm:
        return dict(existing_rm)

    if hash_map is not None:
        entry = hash_map.get(entry_id)
        return dict(entry) if entry else None

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
            f"""SELECT * FROM {SCHEMA_TIMER}.duplicate_reviews
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
            f"""UPDATE {SCHEMA_TIMER}.duplicate_reviews
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


def apply_responses(db, responses: list[dict]) -> list[dict]:
    """Store corrections in app_timer.corrections, removals in app_timer.entry_removals.

    Correction overrides removal — if the same entry is later corrected, the
    removal row stays but rebuild_timer_clean() keeps the entry (correction wins).

    Dedup: Google Sheets sometimes duplicates a form submission into multiple rows.
    We skip a response only when its values match what is already stored
    (exact-duplicate GSheet row). A genuine re-correction — same entry_id but
    a different corrected_duration_min, or flipping edit <-> remove — is allowed
    through to the ON CONFLICT DO UPDATE path so techs can revise their own fix.

    Returns the list of changes actually applied this run, one dict per processed
    response: {entry_id, action, user_email, entry_date (ET date), entry,
    original_duration_min, corrected_duration_min}. Empty list when nothing new.
    """
    now = datetime.now(timezone.utc)
    applied = 0
    applied_changes: list[dict] = []

    # Pre-fetch existing corrections (with values) and removals so we can detect
    # exact-duplicate GSheet rows vs genuine re-corrections.
    all_ids = [r["entry_id"] for r in responses]
    existing_corrections = retry_db(
        lambda: db.fetch(
            f"SELECT entry_id, corrected_duration_min FROM {SCHEMA_TIMER}.corrections WHERE entry_id = ANY($1)",
            all_ids,
        ),
        description="batch check existing corrections",
    )
    existing_removals = retry_db(
        lambda: db.fetch(
            f"SELECT entry_id FROM {SCHEMA_TIMER}.entry_removals WHERE entry_id = ANY($1)",
            all_ids,
        ),
        description="batch check existing removals",
    )
    stored_correction_values = {
        r["entry_id"]: (float(r["corrected_duration_min"]) if r["corrected_duration_min"] is not None else None)
        for r in (existing_corrections or [])
    }
    stored_removal_ids = {r["entry_id"] for r in (existing_removals or [])}

    def _is_exact_duplicate(resp: dict) -> bool:
        eid = resp["entry_id"]
        act = resp["action"]
        if act == "remove":
            return eid in stored_removal_ids
        if act == "correct":
            stored = stored_correction_values.get(eid)
            new_dur = resp.get("corrected_duration_min")
            if stored is None or new_dur is None:
                return False
            return abs(float(new_dur) - stored) < 0.001
        return False

    new_responses = [r for r in responses if not _is_exact_duplicate(r)]
    if len(responses) > len(new_responses):
        logger.info(f"Skipping {len(responses) - len(new_responses)} exact-duplicate responses "
                     f"(GSheet duplicate rows), {len(new_responses)} to process")

    # Resolve all hashes not already stored in ONE table scan (each per-row
    # scan is a full-table MD5 recompute; permanently-unmatched sheet rows
    # otherwise re-scan the table every run and blow the job timeout).
    scan_ids = [
        r["entry_id"] for r in new_responses
        if r["entry_id"] not in stored_correction_values
        and r["entry_id"] not in stored_removal_ids
    ]
    hash_map = lookup_entries_by_hash(db, sorted(set(scan_ids)))

    for resp in new_responses:
        entry_id = resp["entry_id"]
        action = resp["action"]
        corrected_duration = resp.get("corrected_duration_min")
        reason = resp["reason"]

        entry = lookup_entry_by_id(db, entry_id, hash_map=hash_map)
        if not entry:
            logger.warning(f"No timer entry found for entry_id={entry_id}, skipping")
            continue

        start_time = entry["start_time"]
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        if action == "correct":
            corrected_end_time = start_time + timedelta(minutes=corrected_duration)

            # Upsert into app_timer.corrections (entry_id is UNIQUE — last wins)
            retry_db(
                lambda eid=entry_id, e=entry, cd=corrected_duration, cet=corrected_end_time, r=reason: db.execute(
                    f"""INSERT INTO {SCHEMA_TIMER}.corrections
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

            applied_changes.append({
                "entry_id": entry_id,
                "action": "correct",
                "user_email": entry["user_email"],
                "entry_date": _entry_date_et(entry["start_time"]),
                "entry": entry,
                "original_duration_min": entry.get("duration_min"),
                "corrected_duration_min": corrected_duration,
            })

        else:
            # Upsert into app_timer.entry_removals (entry_id is UNIQUE — last wins)
            retry_db(
                lambda eid=entry_id, e=entry, r=reason: db.execute(
                    f"""INSERT INTO {SCHEMA_TIMER}.entry_removals
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

            applied_changes.append({
                "entry_id": entry_id,
                "action": "remove",
                "user_email": entry["user_email"],
                "entry_date": _entry_date_et(entry["start_time"]),
                "entry": entry,
                "original_duration_min": entry.get("duration_min"),
                "corrected_duration_min": None,
            })

        applied += 1

        # Auto-resolve any related duplicate group
        _resolve_duplicate_for_action(db, entry, action, now)

    if applied:
        logger.info(f"Applied {applied} responses, rebuilding clean table...")
        rebuild_clean_table(db)
    else:
        logger.info("No new responses to apply")

    return applied_changes


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
    """Auto-resolve duplicate groups older than AUTO_RESOLVE_DAYS: keep longest duration.

    Short entries are typically system-generated phantom timers from Swift
    mobile/desktop sync issues. The real work session has the longest duration.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=AUTO_RESOLVE_DAYS)

    stale = retry_db(
        lambda: db.fetch(
            f"""SELECT * FROM {SCHEMA_TIMER}.duplicate_reviews
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
        best = max(entries, key=lambda e: float(e.get("duration_min") or 0))
        selection = best["label"]
        rejected = [{"end_time": e.get("end_time"), "duration_min": e.get("duration_min")}
                     for e in entries if e["label"] != selection]

        retry_db(
            lambda gid=review["group_id"], sel=selection, rej=rejected: db.execute(
                f"""UPDATE {SCHEMA_TIMER}.duplicate_reviews
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


# --------------------------------------------------------------------------
# Correction Confirmation Emails — reply-in-thread after --apply processes changes
# --------------------------------------------------------------------------

_STATUS_BADGE_HTML = {
    "unchanged": ('<span style="display:inline-block;background:#9e9e9e;color:white;'
                  'font-size:10px;padding:2px 7px;border-radius:3px;font-weight:bold;">UNCHANGED</span>'),
    "edited":    ('<span style="display:inline-block;background:#1565c0;color:white;'
                  'font-size:10px;padding:2px 7px;border-radius:3px;font-weight:bold;">EDITED</span>'),
    "removed":   ('<span style="display:inline-block;background:#c62828;color:white;'
                  'font-size:10px;padding:2px 7px;border-radius:3px;font-weight:bold;">REMOVED</span>'),
    "added":     ('<span style="display:inline-block;background:#2e7d32;color:white;'
                  'font-size:10px;padding:2px 7px;border-radius:3px;font-weight:bold;">ADDED</span>'),
}


def _fetch_classified_day_entries(db, user_email: str, entry_date) -> list[dict]:
    """Fetch all timer entries for (user_email, entry_date in ET) and
    classify each as UNCHANGED / EDITED / ADDED / REMOVED.

    The surviving set is sourced directly from stg_timer_activities_clean
    (the corrected, deduplicated, removals-excluded table that the exports
    and per-tech emails also read), so this view always agrees with the
    clean table. Removals are unioned in afterwards for context.

    This deliberately does NOT match on the volatile md5 entry_id hash.
    That hash includes end_time + duration_min, so an entry edited while
    its timer was still running (end_time NULL, duration 0) gets a
    different hash once the timer completes, which used to orphan the
    correction and drop the surviving edit from the email. Matching the
    clean table by its natural key avoids that entirely.
    """
    rows = retry_db(
        lambda: db.fetch(f"""
            WITH surviving AS (
                SELECT t.project_did, t.project, t.user_email,
                       t.start_time, t.end_time, t.duration_min,
                       t.site_name, t.site_id, t.task, t.task_clean
                FROM {SCHEMA_STAGING}.stg_timer_activities_clean t
                WHERE t.user_email = $1
                  AND DATE(t.start_time AT TIME ZONE 'America/New_York') = $2
            )
            SELECT s.project_did, s.project, s.user_email,
                   s.start_time, s.end_time, s.duration_min,
                   s.site_name, s.site_id, s.task, s.task_clean,
                   (SELECT c.original_duration_min
                      FROM {SCHEMA_TIMER}.corrections c
                     WHERE c.status = 'corrected'
                       AND c.project_did = s.project_did
                       AND c.user_email = s.user_email
                       AND c.start_time = s.start_time
                       AND c.site_name IS NOT DISTINCT FROM s.site_name
                       AND c.site_id   IS NOT DISTINCT FROM s.site_id
                       AND c.task      IS NOT DISTINCT FROM s.task
                       AND c.corrected_duration_min IS NOT DISTINCT FROM s.duration_min
                       AND c.corrected_end_time     IS NOT DISTINCT FROM s.end_time
                     LIMIT 1) AS corr_orig,
                   EXISTS (SELECT 1
                      FROM {SCHEMA_TIMER}.corrections c
                     WHERE c.status = 'corrected'
                       AND c.project_did = s.project_did
                       AND c.user_email = s.user_email
                       AND c.start_time = s.start_time
                       AND c.site_name IS NOT DISTINCT FROM s.site_name
                       AND c.site_id   IS NOT DISTINCT FROM s.site_id
                       AND c.task      IS NOT DISTINCT FROM s.task
                       AND c.corrected_duration_min IS NOT DISTINCT FROM s.duration_min
                       AND c.corrected_end_time     IS NOT DISTINCT FROM s.end_time) AS is_edited,
                   EXISTS (SELECT 1
                      FROM {SCHEMA_TIMER}.entry_additions a
                     WHERE a.project_did = s.project_did
                       AND a.user_email = s.user_email
                       AND a.start_time = s.start_time
                       AND a.site_name IS NOT DISTINCT FROM s.site_name
                       AND a.site_id   IS NOT DISTINCT FROM s.site_id
                       AND a.task      IS NOT DISTINCT FROM s.task
                       AND a.duration_min IS NOT DISTINCT FROM s.duration_min) AS is_added,
                   FALSE AS is_removed
            FROM surviving s

            UNION ALL

            SELECT rm.project_did, rm.project, rm.user_email,
                   rm.start_time, rm.end_time, rm.duration_min,
                   rm.site_name, rm.site_id, rm.task,
                   NULL::text AS task_clean,
                   NULL::numeric AS corr_orig,
                   FALSE AS is_edited,
                   FALSE AS is_added,
                   TRUE  AS is_removed
            FROM {SCHEMA_TIMER}.entry_removals rm
            WHERE rm.user_email = $1
              AND DATE(rm.start_time AT TIME ZONE 'America/New_York') = $2
              AND rm.reason IS DISTINCT FROM 'REVERTED'

            ORDER BY start_time, site_name, task
        """, user_email, entry_date),
        description=f"classify entries for {user_email} on {entry_date}",
    )

    classified = []
    for r in rows:
        d = dict(r)
        surviving_dur = d.get("duration_min")
        if d.get("is_removed"):
            status = "removed"
            effective_duration = 0.0
            effective_end = d.get("end_time")
            corrected_dur = None
            original_dur = surviving_dur
        elif d.get("is_added"):
            status = "added"
            effective_duration = float(surviving_dur or 0)
            effective_end = d.get("end_time")
            corrected_dur = None
            original_dur = surviving_dur
        elif d.get("is_edited"):
            status = "edited"
            effective_duration = float(surviving_dur or 0)
            effective_end = d.get("end_time")
            corrected_dur = surviving_dur
            original_dur = d.get("corr_orig")
        else:
            status = "unchanged"
            effective_duration = float(surviving_dur or 0)
            effective_end = d.get("end_time")
            corrected_dur = None
            original_dur = surviving_dur
        d["original_duration_min"] = original_dur
        d["corrected_duration_min"] = corrected_dur
        d["status"] = status
        d["effective_duration_min"] = effective_duration
        d["effective_end_time"] = effective_end
        classified.append(d)
    return classified


def _has_edits(classified_entries: list[dict]) -> bool:
    return any(e["status"] == "edited" for e in classified_entries)


def _has_removals(classified_entries: list[dict]) -> bool:
    return any(e["status"] == "removed" for e in classified_entries)


def _has_additions(classified_entries: list[dict]) -> bool:
    return any(e["status"] == "added" for e in classified_entries)


def _build_confirmation_summary_html(classified_entries: list[dict]) -> str:
    """Daily Task Summary for the confirmation email.

    Excludes removed entries; uses effective (post-correction) durations.
    No duplicates column — duplicates are strictly a --remind concern.
    Ends with a bold 'Day Total' row.
    """
    from collections import defaultdict

    effective = [e for e in classified_entries if e["status"] != "removed"]
    if not effective:
        return ""

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for e in effective:
        task = e.get("task_clean") or e.get("task") or ""
        key = (task, e.get("site_name") or "", e.get("project") or "")
        buckets[key].append(e)

    groups = []
    for (task, site, project), rows in buckets.items():
        groups.append({
            "project": project,
            "site": site,
            "task": task,
            "entries": len(rows),
            "total_duration_min": sum(float(r.get("effective_duration_min") or 0) for r in rows),
        })
    groups.sort(key=lambda g: (g["project"], g["site"], g["task"]))

    header_style = ("padding:6px 10px;border:1px solid #bbb;background:#eef3fa;"
                    "text-align:left;font-size:13px;")
    cell_style = "padding:6px 10px;border:1px solid #ccc;font-size:13px;"

    total_entries = sum(g["entries"] for g in groups)
    total_duration = sum(g["total_duration_min"] for g in groups)

    html = [
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;margin:8px 0 16px;">',
        '<tr>',
        f'<th style="{header_style}">Project</th>',
        f'<th style="{header_style}">Site</th>',
        f'<th style="{header_style}">Task</th>',
        f'<th style="{header_style}text-align:right;">Entries</th>',
        f'<th style="{header_style}text-align:right;">Total</th>',
        '</tr>',
    ]
    for g in groups:
        html.append(
            '<tr>'
            f'<td style="{cell_style}">{_escape_html(g["project"])}</td>'
            f'<td style="{cell_style}">{_escape_html(g["site"])}</td>'
            f'<td style="{cell_style}">{_escape_html(g["task"])}</td>'
            f'<td style="{cell_style}text-align:right;">{g["entries"]}</td>'
            f'<td style="{cell_style}text-align:right;">{_fmt_duration(g["total_duration_min"])}</td>'
            '</tr>'
        )
    html.append(
        '<tr style="background:#f0f4f9;">'
        f'<td colspan="3" style="{cell_style}font-weight:bold;">Day Total</td>'
        f'<td style="{cell_style}text-align:right;font-weight:bold;">{total_entries}</td>'
        f'<td style="{cell_style}text-align:right;font-weight:bold;">{_fmt_duration(total_duration)}</td>'
        '</tr>'
    )
    html.append('</table>')
    return "".join(html)


def _build_confirmation_entries_html(classified_entries: list[dict]) -> str:
    """Full entry detail table for the confirmation email.

    Each row shows its status badge and duration. Edited rows show
    'before -> after' with strikethrough + green after. Removed rows are
    shown struck through with muted background.
    """
    cell = "padding:6px 10px;border:1px solid #ddd;"
    rows_html = []

    for e in classified_entries:
        status = e["status"]
        project = e.get("project") or "(no project)"
        site = e.get("site_name") or "(no site)"
        task = e.get("task") or "(no task)"
        start = _fmt_time_short(e["start_time"])
        end = _fmt_time_short(e.get("effective_end_time"))

        if status == "removed":
            row_style = "background:#fdecea;color:#999;"
            strike_cell = f"{cell}text-decoration:line-through;"
            rows_html.append(
                f'<tr style="{row_style}">'
                f'<td style="{cell}">{_STATUS_BADGE_HTML["removed"]}</td>'
                f'<td style="{strike_cell}">{_escape_html(project)}</td>'
                f'<td style="{strike_cell}">{_escape_html(site)}</td>'
                f'<td style="{strike_cell}">{_escape_html(task)}</td>'
                f'<td style="{strike_cell}">{start}</td>'
                f'<td style="{strike_cell}">{end}</td>'
                f'<td style="{strike_cell}">{_fmt_duration(e.get("original_duration_min"))}</td>'
                f'</tr>'
            )
        elif status == "added":
            row_style = "background:#e8f5e9;"
            rows_html.append(
                f'<tr style="{row_style}">'
                f'<td style="{cell}">{_STATUS_BADGE_HTML["added"]}</td>'
                f'<td style="{cell}">{_escape_html(project)}</td>'
                f'<td style="{cell}">{_escape_html(site)}</td>'
                f'<td style="{cell}">{_escape_html(task)}</td>'
                f'<td style="{cell}">{start}</td>'
                f'<td style="{cell}">{end}</td>'
                f'<td style="{cell}font-weight:bold;color:#2e7d32;">{_fmt_duration(e.get("original_duration_min"))}</td>'
                f'</tr>'
            )
        elif status == "edited":
            row_style = "background:#e3f2fd;"
            before = _fmt_duration(e.get("original_duration_min"))
            after = _fmt_duration(e.get("corrected_duration_min"))
            duration_cell = (
                f'<td style="{cell}">'
                f'<span style="color:#888;text-decoration:line-through;">{before}</span>'
                f'<span style="color:#555;">&nbsp;&rarr;&nbsp;</span>'
                f'<span style="font-weight:bold;color:#2e7d32;">{after}</span>'
                f'</td>'
            )
            rows_html.append(
                f'<tr style="{row_style}">'
                f'<td style="{cell}">{_STATUS_BADGE_HTML["edited"]}</td>'
                f'<td style="{cell}">{_escape_html(project)}</td>'
                f'<td style="{cell}">{_escape_html(site)}</td>'
                f'<td style="{cell}">{_escape_html(task)}</td>'
                f'<td style="{cell}">{start}</td>'
                f'<td style="{cell}">{end}</td>'
                f'{duration_cell}'
                f'</tr>'
            )
        else:  # unchanged
            rows_html.append(
                '<tr>'
                f'<td style="{cell}">{_STATUS_BADGE_HTML["unchanged"]}</td>'
                f'<td style="{cell}">{_escape_html(project)}</td>'
                f'<td style="{cell}">{_escape_html(site)}</td>'
                f'<td style="{cell}">{_escape_html(task)}</td>'
                f'<td style="{cell}">{start}</td>'
                f'<td style="{cell}">{end}</td>'
                f'<td style="{cell}font-weight:bold;">{_fmt_duration(e.get("original_duration_min"))}</td>'
                '</tr>'
            )
    return "\n".join(rows_html)


def _build_correction_confirmation_html(user_email: str, entry_date,
                                         classified_entries: list[dict],
                                         change_count: int,
                                         edit_count: int,
                                         removal_count: int,
                                         added_count: int = 0) -> str:
    """Render the confirmation email HTML body for a single (user, date)."""
    date_str = entry_date.strftime("%B %d, %Y")
    summary_html = _build_confirmation_summary_html(classified_entries)
    entries_html = _build_confirmation_entries_html(classified_entries)
    has_edits = _has_edits(classified_entries)
    has_removals = _has_removals(classified_entries)
    has_additions = _has_additions(classified_entries)
    has_unchanged = any(e["status"] == "unchanged" for e in classified_entries)

    # Subheader: "N changes applied — X edited, Y removed, Z added"
    sub_parts = []
    if edit_count:
        sub_parts.append(f"{edit_count} edited")
    if removal_count:
        sub_parts.append(f"{removal_count} removed")
    if added_count:
        sub_parts.append(f"{added_count} added")
    detail = " &mdash; " + ", ".join(sub_parts) if sub_parts else ""
    subheader = f"{change_count} change{'s' if change_count != 1 else ''} applied{detail}"

    # Legend — only include badges that actually appear in this email
    legend_items = []
    if has_unchanged:
        legend_items.append(f'<li>{_STATUS_BADGE_HTML["unchanged"]} &mdash; no correction submitted</li>')
    if has_edits:
        legend_items.append(f'<li>{_STATUS_BADGE_HTML["edited"]} &mdash; duration corrected (before &rarr; after shown)</li>')
    if has_removals:
        legend_items.append(f'<li>{_STATUS_BADGE_HTML["removed"]} &mdash; entry deleted from your records</li>')
    if has_additions:
        legend_items.append(f'<li>{_STATUS_BADGE_HTML["added"]} &mdash; entry added manually (e.g., forgot to start the timer)</li>')
    legend_html = "\n".join(legend_items)

    # Conditional footer notes (same pattern as the daily email fix)
    footer_items = []
    if has_edits:
        footer_items.append("<li><strong>Edited</strong> rows show the original duration struck through and the new duration in green.</li>")
    if has_removals:
        footer_items.append("<li><strong>Removed</strong> rows are shown for context but no longer count toward your daily totals.</li>")
    if has_additions:
        footer_items.append("<li><strong>Added</strong> rows were created manually for entries missing from the timer (e.g., the timer wasn't started). They count toward your daily totals.</li>")
    footer_items.append("<li>You can still make further corrections from the <strong>original daily entries email</strong> &mdash; the Edit/Remove links remain valid.</li>")
    footer_html = "\n".join(footer_items)

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
        <div style="background:#2e7d32;color:white;padding:16px 24px;">
            <h2 style="margin:0;">Timer Entries Updated - {date_str}</h2>
            <p style="margin:4px 0 0;font-size:13px;opacity:0.9;">{subheader}</p>
        </div>
        <div style="padding:24px;">

            <p>Hi {_first_name(user_email)},</p>
            <p>Your timer entries for <strong>{date_str}</strong> were updated based on the corrections you submitted. Below is the full updated view of your day &mdash; unchanged entries, edits, and removals are all shown for context.</p>

            <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Updated Daily Task Summary</h3>
            <p style="margin:0 0 6px;font-size:12px;color:#666;">Totals reflect your corrections and removals.</p>
            {summary_html}

            <h3 style="margin-top:24px;margin-bottom:8px;font-size:15px;">Updated Entry Details</h3>
            <ul style="font-size:13px;color:#555;margin:8px 0 12px;">
                {legend_html}
            </ul>
            <table style="border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 16px;">
                <thead>
                    <tr style="background:#f5f5f5;">
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Status</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Project</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Site</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Task</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Start</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">End</th>
                        <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Duration</th>
                    </tr>
                </thead>
                <tbody>
                    {entries_html}
                </tbody>
            </table>

            <div style="background:#f5f5f5;border-radius:6px;padding:14px 18px;margin-top:24px;font-size:13px;color:#555;">
                <p style="margin:0 0 8px;font-weight:bold;color:#333;">A few things to note:</p>
                <ul style="margin:0;padding-left:20px;line-height:1.8;">
                    {footer_html}
                </ul>
            </div>

        </div>
    </body>
    </html>
    """


def send_correction_confirmations(db, applied_changes: list[dict], test_mode: bool = False):
    """Send a reply-in-thread confirmation email per (user, entry_date) for
    changes applied this run. Threads under the original daily entries email
    using thread_id/message_id stored in app_timer.daily_notifications.
    Falls back to standalone email when no thread record exists.
    """
    if not applied_changes:
        logger.info("No applied changes — skipping correction confirmations")
        return

    from gmail_client import authenticate

    by_user_date: dict[tuple, list[dict]] = {}
    for change in applied_changes:
        key = (change["user_email"], change["entry_date"])
        by_user_date.setdefault(key, []).append(change)

    service = authenticate()
    sent = 0

    for (user_email, entry_date), changes in by_user_date.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        date_str = entry_date.strftime("%B %d, %Y")
        edit_count = sum(1 for c in changes if c["action"] == "correct")
        removal_count = sum(1 for c in changes if c["action"] == "remove")
        added_count = sum(1 for c in changes if c["action"] == "add")

        notif = retry_db(
            lambda ue=user_email, sd=entry_date: db.fetchrow(
                f"""SELECT thread_id, message_id
                    FROM {SCHEMA_TIMER}.daily_notifications
                    WHERE user_email = $1 AND send_date = $2
                """, ue, sd,
            ),
            description=f"lookup notification thread for {user_email} on {entry_date}",
        )

        classified = _fetch_classified_day_entries(db, user_email, entry_date)
        if not classified:
            logger.warning(f"No entries found for {user_email} on {entry_date} — skipping confirmation")
            continue

        html_body = _build_correction_confirmation_html(
            user_email, entry_date, classified,
            change_count=len(changes),
            edit_count=edit_count,
            removal_count=removal_count,
            added_count=added_count,
        )

        subject = f"Re: Timer Activity Entries - {date_str}"
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
            sent += 1
            logger.info(
                f"Sent correction confirmation to {recipient} for {entry_date} "
                f"({edit_count} edited, {removal_count} removed, {added_count} added, "
                f"thread={notif.get('thread_id') if notif else 'none'})"
            )
        except Exception as e:
            logger.error(
                f"Failed to send correction confirmation to {user_email} for {entry_date}: {e}"
            )

    logger.info(f"Correction confirmations complete: sent {sent} emails")


def run_apply(test_mode: bool = False):
    """Process form responses, auto-resolve stale duplicates, rebuild clean table,
    then send reply-in-thread confirmation emails for any changes just applied.
    """
    db = get_db()

    # 1. Process form responses (returns list of changes actually applied)
    applied_changes: list[dict] = []
    responses = read_form_responses()
    if responses:
        applied_changes = apply_responses(db, responses)

    # 2. Auto-resolve stale duplicate groups
    auto_resolved = auto_resolve_stale(db)

    # 3. Always rebuild — picks up new staging data from tonight's timer extract.
    # apply_responses() already rebuilds when it applies new corrections (applied > 0),
    # but we rebuild unconditionally here to ensure the clean table always reflects
    # tonight's fresh extraction data, even when no new corrections were applied.
    rebuild_clean_table(db)

    # 4. Send reply-in-thread confirmation emails for this run's applied changes
    if applied_changes:
        send_correction_confirmations(db, applied_changes, test_mode=test_mode)


# --------------------------------------------------------------------------
# --remind: Send reminders for unresolved duplicate groups
# --------------------------------------------------------------------------

def run_remind(test_mode: bool = False):
    """Send reminder emails for unresolved duplicate groups.

    Sends one reminder per (user, date) so each threads with the correct
    daily entries email.  Falls back to standalone if no notification record
    exists for that date.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    unresolved = retry_db(
        lambda: db.fetch(
            f"""SELECT * FROM {SCHEMA_TIMER}.duplicate_reviews
                WHERE status IN ('pending', 'notified')
                  AND notified_at IS NOT NULL
                  AND (last_reminder_at IS NULL
                       OR last_reminder_at < NOW() - INTERVAL '20 hours')
            """,
        ),
        description="find unresolved duplicate reviews needing reminder",
    )

    if not unresolved:
        logger.info("No unresolved duplicate reviews need reminders")
        return

    from gmail_client import authenticate

    # Group by (user_email, entry_date) so each reminder is per-date
    by_user_date = {}
    for r in unresolved:
        entries = r["entries"] if isinstance(r["entries"], list) else json.loads(r["entries"])
        days_pending = (now - r["notified_at"]).days
        st = r["start_time"]
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        entry_date = st.astimezone(TZ_EASTERN).date()
        key = (r["user_email"], entry_date)
        by_user_date.setdefault(key, []).append({
            "group_id": r["group_id"],
            "project": r["project"],
            "site_name": r["site_name"],
            "task": r["task"],
            "start_time": r["start_time"],
            "entries": entries,
            "days_pending": days_pending,
        })

    # In test mode, limit to dates that have notification records (for threading)
    # to avoid spamming jamil with hundreds of standalone reminders
    if test_mode:
        dates_with_notifs = set()
        notif_dates = retry_db(
            lambda: db.fetch(
                f"SELECT DISTINCT send_date FROM {SCHEMA_TIMER}.daily_notifications"
            ),
            description="get dates with notification records",
        )
        if notif_dates:
            dates_with_notifs = {r["send_date"] for r in notif_dates}
        original_count = len(by_user_date)
        by_user_date = {k: v for k, v in by_user_date.items() if k[1] in dates_with_notifs}
        skipped = original_count - len(by_user_date)
        if skipped:
            logger.info(f"Test mode: skipped {skipped} reminders for dates without notification records")

    service = authenticate()
    all_group_ids = []

    for (user_email, entry_date), groups in by_user_date.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(groups)
        max_days = max(g["days_pending"] for g in groups)
        days_left = max(0, AUTO_RESOLVE_DAYS - max_days)

        # Look up notification thread for this specific (user, date)
        notif = retry_db(
            lambda ue=user_email, sd=entry_date: db.fetchrow(
                f"""SELECT thread_id, message_id, send_date
                    FROM {SCHEMA_TIMER}.daily_notifications
                    WHERE user_email = $1 AND send_date = $2
                """, ue, sd,
            ),
            description=f"lookup notification thread for {user_email} on {entry_date}",
        )

        date_str = entry_date.strftime("%B %d, %Y")

        # Build summary list of duplicate groups
        summary_items = []
        for g in groups:
            site = g.get("site_name") or "(no site)"
            task = g.get("task") or "(no task)"
            days = g["days_pending"]
            n_entries = len(g["entries"])
            start_time = _fmt_time_short(g["start_time"])
            summary_items.append(
                f'<li>{site} &mdash; {task} '
                f'(Start: {start_time}, {n_entries} entries, '
                f'<span style="color:#c62828;">{days} day{"s" if days != 1 else ""} pending</span>)</li>'
            )
        summary_html = "\n".join(summary_items)

        auto_resolve_warning = (
            f"Duplicate entries will be auto-resolved in <strong>{days_left} day{'s' if days_left != 1 else ''}</strong> "
            f"(longest duration kept)."
            if days_left > 0
            else "Duplicate entries will be <strong>auto-resolved today</strong> (longest duration kept)."
        )

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#e65100;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Duplicate Reminder - {date_str}</h2>
                <p style="margin:4px 0 0;font-size:13px;opacity:0.9;">{max_days} day{'s' if max_days != 1 else ''} pending</p>
            </div>
            <div style="padding:24px;">
                <p>Hi {_first_name(user_email)},</p>
                <p>You have <strong>{n}</strong> duplicate timer
                   {'group' if n == 1 else 'groups'} from <strong>{date_str}</strong>
                   that still need attention.
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

        # Subject matches original daily email for Gmail threading
        subject = f"Re: Timer Activity Entries - {date_str}"

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
            logger.info(f"Sent duplicate reminder to {recipient} for {entry_date} "
                        f"({n} groups, thread={notif.get('thread_id') if notif else 'none'})")
        except Exception as e:
            logger.error(f"Failed to send reminder to {user_email} for {entry_date}: {e}")

        all_group_ids.extend(g["group_id"] for g in groups)

    # Update reminder counts
    if all_group_ids:
        retry_db(
            lambda: db.execute(
                f"""UPDATE {SCHEMA_TIMER}.duplicate_reviews
                    SET reminder_count = reminder_count + 1, last_reminder_at = $1, updated_at = $1
                    WHERE group_id = ANY($2)
                """, now, all_group_ids,
            ),
            description="update reminder counts",
        )
    total_groups = len(all_group_ids)
    total_emails = len(by_user_date)
    logger.info(f"Remind complete: sent {total_emails} reminder emails for {total_groups} groups")


# --------------------------------------------------------------------------
# --resend: Re-send daily email when entries materialize or get added
# --------------------------------------------------------------------------

def _make_resend_stable_key(project_did, user_email, start_time,
                            site_name=None, site_id=None, task=None,
                            end_time=None) -> str:
    """Stable 12-char hash for resend trigger / NEW-badge comparison.

    Why a separate hash from _make_entry_id: NUMERIC duration_min carries
    a long precision tail (e.g. 19.6000000000000014...) that the timer
    pipeline can re-emit with a different tail after its monthly
    DELETE+REINSERT, even when the row didn't semantically change. That
    flips _make_entry_id's md5, which triggered ~10 false-positive
    resend emails per night. This key drops duration_min entirely and
    truncates timestamps to second precision, so resend only fires on
    real changes: NULL end_time -> set, or a brand-new (start_time,
    task, site) combination.
    """
    def _sec(dt):
        if dt is None:
            return "None"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S+00")
    parts = [
        str(project_did), str(user_email), _sec(start_time),
        str(site_name) if site_name is not None else "None",
        str(site_id) if site_id is not None else "None",
        str(task) if task is not None else "None",
        _sec(end_time),
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def _collect_entry_ids(entries: list[dict]) -> list[str]:
    """Return the list of 12-char STABLE resend keys for the given entry
    dicts. Used to track which entries were in the last-sent daily email
    and detect changes for the resend pass. NOTE: these are NOT the same
    hashes as app_timer.corrections.entry_id / app_timer.entry_removals.
    entry_id — those use _make_entry_id (full natural key). See
    _make_resend_stable_key for the rationale.
    """
    return [
        _make_resend_stable_key(
            e["project_did"], e["user_email"], e["start_time"],
            e.get("site_name"), e.get("site_id"), e.get("task"),
            e.get("end_time"),
        )
        for e in entries
    ]


def _fetch_current_day_entries(db, user_email: str, entry_date) -> list[dict]:
    """Fetch the canonical view of (user, date) entries for re-send from
    stg_timer_activities_clean. Each row carries an `is_edited` flag set
    to True when a correction's corrected natural key matches the row —
    which covers both rows that had a correction applied in-place (Step 2
    of rebuild_timer_clean) AND orphaned corrections that were injected
    as virtual rows (Step 4, migration 051). Removed rows are already
    filtered out by Step 1; manual additions are included by Step 3.

    Snapshot-gating note: pulling from clean means a tech-submitted
    correction WILL trigger a re-send the next nightly because the
    entry_id hash changes when end_time / duration_min change. That's
    the desired behavior — the confirmation email shows "what changed",
    the resend shows the full corrected day with current Edit / Remove
    buttons. Two emails serve different purposes.
    """
    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT
                c.project_did, c.project, c.user_email,
                c.start_time, c.end_time, c.duration_min,
                c.site_name, c.site_id, c.task, c.task_clean,
                (corr.id IS NOT NULL) AS is_edited
            FROM {SCHEMA_STAGING}.stg_timer_activities_clean c
            LEFT JOIN {SCHEMA_TIMER}.corrections corr
                ON corr.status = 'corrected'
               AND corr.project_did = c.project_did
               AND corr.user_email  = c.user_email
               AND corr.start_time  = c.start_time
               AND corr.site_name IS NOT DISTINCT FROM c.site_name
               AND corr.site_id   IS NOT DISTINCT FROM c.site_id
               AND corr.task      IS NOT DISTINCT FROM c.task
               AND corr.corrected_end_time IS NOT DISTINCT FROM c.end_time
               AND corr.corrected_duration_min IS NOT DISTINCT FROM c.duration_min
            WHERE c.user_email = $1
              AND DATE(c.start_time AT TIME ZONE 'America/New_York') = $2
            ORDER BY c.start_time, c.site_name, c.task
        """, user_email, entry_date),
        description=f"current-day entries for {user_email} on {entry_date}",
    )
    return [dict(r) for r in rows] if rows else []


def find_days_needing_resend(db, lookback_days: int = 7) -> list[dict]:
    """Return (user_email, send_date, thread_id, message_id, last_sent_entry_ids,
    current_entries) for every (user, date) in the last `lookback_days` days
    whose current entry-id set differs from the stored snapshot.

    First-time bootstrap: rows with NULL last_sent_entry_ids are populated
    with the current set silently and NOT returned as resend candidates,
    so we don't blast a re-send to every tech the day after this migration.
    """
    cutoff = (datetime.now(TZ_EASTERN) - timedelta(days=lookback_days)).date()
    # Surface unexpectedly large batches — a clean steady state is a
    # handful of candidates per day. A spike usually means an upstream
    # extractor reloaded a big window or the lookback was widened by
    # mistake.
    LARGE_BATCH_THRESHOLD = 30
    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT user_email, send_date, thread_id, message_id,
                   last_sent_at, last_sent_entry_ids
            FROM {SCHEMA_TIMER}.daily_notifications
            WHERE send_date >= $1
              AND thread_id IS NOT NULL
            ORDER BY send_date DESC, user_email
        """, cutoff),
        description="fetch resend candidates",
    )
    if not rows:
        return []

    candidates = []
    for r in rows:
        user_email = r["user_email"]
        send_date = r["send_date"]
        current = _fetch_current_day_entries(db, user_email, send_date)
        if not current:
            continue
        # Pure corrections (is_edited) already trigger a "Timer Entries
        # Updated" confirmation email — re-sending the whole day would be
        # a redundant second notification. Drop them from the trigger set
        # so only truly-new rows (manual additions, NULL-end-time timers
        # that completed) cause a resend. Edited rows still render with
        # the EDITED badge in the body when a resend does fire for some
        # other reason on the same day.
        non_edited = [e for e in current if not e.get("is_edited")]
        current_ids = set(_collect_entry_ids(non_edited))
        snapshot = r["last_sent_entry_ids"]

        if snapshot is None:
            # Bootstrap: never sent with snapshot before. Populate quietly
            # so the next change actually triggers a re-send. Store the
            # FULL unfiltered current set (not current_ids, which is
            # non-edited only). Snapshot semantics = "every stable key
            # in the day as of last send"; the non_edited filter is a
            # trigger-time concern. send_resend_emails uses the same
            # full-set convention at its post-send snapshot update,
            # so bootstrap matching it keeps the two writes consistent.
            # Without this, an edited row whose correction is later
            # deleted would falsely re-appear as NEW in a future resend.
            bootstrap_ids = sorted(_collect_entry_ids(current))
            retry_db(
                lambda ue=user_email, sd=send_date, ids=bootstrap_ids: db.execute(
                    f"""UPDATE {SCHEMA_TIMER}.daily_notifications
                        SET last_sent_entry_ids = $1::jsonb
                        WHERE user_email = $2 AND send_date = $3
                    """,
                    ids, ue, sd,
                ),
                description=f"bootstrap snapshot for {user_email} on {send_date}",
            )
            continue

        # `snapshot` arrives as a Python list (asyncpg decodes JSONB → list).
        snapshot_ids = set(snapshot) if isinstance(snapshot, (list, set)) else set(json.loads(snapshot))

        # Trigger only when NEW entry_ids appeared. Removed ones don't
        # warrant a re-send — confirmation emails cover that path.
        if current_ids - snapshot_ids:
            candidates.append({
                "user_email": user_email,
                "send_date": send_date,
                "thread_id": r["thread_id"],
                "message_id": r["message_id"],
                "snapshot_ids": snapshot_ids,
                "current_entries": current,
            })

    if len(candidates) > LARGE_BATCH_THRESHOLD:
        logger.warning(
            f"Large re-send batch: {len(candidates)} (user, date) pairs in "
            f"{lookback_days}-day lookback. Review whether an upstream "
            f"reload triggered this before sending."
        )

    return candidates


def _resend_has_new_rows(entries: list[dict], snapshot_ids: set[str]) -> bool:
    """True when at least one row in the resend will render a NEW badge —
    i.e., is not edited and its entry_id is missing from the snapshot.
    Used to decide whether the 'Why am I getting this email again?'
    callout applies. Edits alone don't justify the callout because the
    tech already knows they submitted them.
    """
    for entry in entries:
        if entry.get("is_edited"):
            continue
        eid = _make_resend_stable_key(
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
            entry.get("end_time"),
        )
        if eid not in snapshot_ids:
            return True
    return False


def _build_resend_entries_html(entries: list[dict], snapshot_ids: set[str]) -> str:
    """Wrap _build_entries_html, appending one optional badge in the
    Duration cell of each row. EDITED takes precedence over NEW because
    it's the more specific signal — the tech already knows they made the
    change, so labeling the row as NEW too would be redundant.

    - EDITED (blue, matches the Edit button color) when the entry's
      `is_edited` flag is True. Set by _fetch_current_day_entries via a
      LEFT JOIN to app_timer.corrections on the corrected natural key.
    - NEW (green) when the entry is not edited AND its entry_id is not
      present in `snapshot_ids` — indicates the row materialized or was
      manually added since the last sent email.
    """
    base_html = _build_entries_html(entries)
    new_badge = (
        ' <span style="display:inline-block;background:#2e7d32;color:white;'
        'font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;'
        'margin-left:4px;font-weight:bold;">NEW</span>'
    )
    edited_badge = (
        ' <span style="display:inline-block;background:#1565c0;color:white;'
        'font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;'
        'margin-left:4px;font-weight:bold;">EDITED</span>'
    )

    rows = base_html.split("<tr")
    out = [rows[0]]
    new_rows_marked = 0
    edited_rows_marked = 0
    for i, entry in enumerate(entries, start=1):
        eid = _make_resend_stable_key(
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
            entry.get("end_time"),
        )
        row_html = "<tr" + rows[i]
        badge = ""
        if entry.get("is_edited"):
            badge = edited_badge
            edited_rows_marked += 1
        elif eid not in snapshot_ids:
            badge = new_badge
            new_rows_marked += 1
        if badge:
            # Splice the badge into the 7th </td> (Duration cell). Cells
            # 1-7 are Date / Project / Site / Task / Start / End / Duration;
            # cell 8 is Action. Index tds[6] is the Duration cell.
            tds = row_html.split("</td>")
            if len(tds) >= 7:
                tds[6] = tds[6] + badge
                row_html = "</td>".join(tds)
        out.append(row_html)

    logger.debug(f"resend HTML: marked {new_rows_marked} rows as NEW, "
                 f"{edited_rows_marked} rows as EDITED")
    return "".join(out)


_RESEND_CALLOUT_HTML = (
    '<div style="background:#f3e5f5;border-left:4px solid #7b1fa2;'
    'border-radius:4px;padding:12px 16px;margin:12px 0 18px;font-size:13px;color:#333;">'
    '<p style="margin:0 0 6px;font-weight:bold;color:#4a148c;">'
    'Why am I getting this email again?</p>'
    '<p style="margin:0;line-height:1.5;">'
    'Some entries on this day have changed since the original email went out. '
    'Entries that were still running when the first email was sent have now '
    'completed and appear below with their final durations. Edit and Remove '
    'buttons reflect the current entries &mdash; please use the links in '
    '<strong>this</strong> email rather than the original.</p>'
    '</div>'
)

_RESEND_UPDATED_BADGE = (
    ' <span style="display:inline-block;background:#7b1fa2;color:white;'
    'font-size:11px;padding:3px 8px;border-radius:3px;vertical-align:middle;'
    'margin-left:6px;letter-spacing:0.5px;">UPDATED</span>'
)


def send_resend_emails(db, test_mode: bool = False, lookback_days: int = 7):
    """Detect (user, date) pairs whose current entry set has changed since
    the last sent email and reply on the original thread with an UPDATED
    view. Threads via app_timer.daily_notifications.thread_id; updates
    last_sent_at + last_sent_entry_ids after each send.
    """
    from gmail_client import authenticate

    candidates = find_days_needing_resend(db, lookback_days=lookback_days)
    if not candidates:
        logger.info("No days need a re-send")
        return

    logger.info(f"Re-send candidates: {len(candidates)} (user, date) pairs")
    service = authenticate()
    sent = 0

    for c in candidates:
        user_email = c["user_email"]
        send_date = c["send_date"]
        thread_id = c["thread_id"]
        message_id = c["message_id"]
        entries = c["current_entries"]
        snapshot_ids = c["snapshot_ids"]
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(entries)
        date_str = send_date.strftime("%B %d, %Y")

        table_rows = _build_resend_entries_html(entries, snapshot_ids)
        has_duplicates = _has_duplicate_entries(entries)
        has_new = _resend_has_new_rows(entries, snapshot_ids)
        callout_html = _RESEND_CALLOUT_HTML if has_new else ""
        duplicate_notes = (
            "<li>You'll receive daily reminders until all duplicate entries are resolved.</li>"
            "<li>The <strong>DUPLICATE</strong> badge marks entries that overlap in time on the same task &mdash; these are likely system-generated duplicates.</li>"
            if has_duplicates else ""
        )

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#1565c0;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Activity Entries - {date_str}{_RESEND_UPDATED_BADGE}</h2>
                <p style="margin:4px 0 0;font-size:13px;opacity:0.9;">Your day was updated since the original email. Latest view below.</p>
            </div>
            <div style="padding:24px;">
                <p>Hi {_first_name(user_email)},</p>
                {callout_html}
                <p>Here are your <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'}
                   from <strong>{date_str}</strong>.</p>

                <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Daily Task Summary</h3>
                {_build_summary_html(entries)}

                <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Entry Details</h3>
                <ul style="font-size:13px;color:#555;margin:8px 0 16px;">
                    <li><strong style="color:#1565c0;">Edit</strong> &mdash; fix a wrong duration</li>
                    <li><strong style="color:#c62828;">Remove</strong> &mdash; delete a duplicate or incorrect entry</li>
                    <li><span style="display:inline-block;background:#2e7d32;color:white;font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;font-weight:bold;">NEW</span> &mdash; entry materialized since the original email (a running timer that has now completed, or a manual addition)</li>
                    <li><span style="display:inline-block;background:#1565c0;color:white;font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;font-weight:bold;">EDITED</span> &mdash; duration reflects an edit you submitted via the daily email, not the raw Swift value</li>
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

                <div style="background:#f5f5f5;border-radius:6px;padding:14px 18px;margin-top:24px;font-size:13px;color:#555;">
                    <p style="margin:0 0 8px;font-weight:bold;color:#333;">A few things to note:</p>
                    <ul style="margin:0;padding-left:20px;line-height:1.8;">
                        <li>Use the Edit and Remove links in <strong>this</strong> email rather than the original &mdash; links in older emails for this day may no longer match the current entries.</li>
                        {duplicate_notes}
                        <li>Entries are <strong>color-coded</strong> by site and task so you can easily spot related groups.</li>
                    </ul>
                    <p style="margin:8px 0 0;color:#888;font-size:12px;">
                        Only click a button if something needs to be changed.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """

        subject = f"Re: Timer Activity Entries - {date_str}"
        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        msg["Subject"] = subject
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = message_id
        msg.attach(MIMEText(html_body, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
            send_body = {"raw": raw}
            if thread_id:
                send_body["threadId"] = thread_id
            service.users().messages().send(userId="me", body=send_body).execute()
            sent += 1

            # Pass the raw list — asyncpg's JSONB codec serializes it as
            # a proper JSONB array, not a JSONB string containing JSON.
            current_ids = sorted(_collect_entry_ids(entries))
            retry_db(
                lambda ue=user_email, sd=send_date, ids=current_ids: db.execute(
                    f"""UPDATE {SCHEMA_TIMER}.daily_notifications
                        SET last_sent_at = NOW(),
                            last_sent_entry_ids = $1::jsonb
                        WHERE user_email = $2 AND send_date = $3
                    """,
                    ids, ue, sd,
                ),
                description=f"update last_sent for {user_email} on {send_date}",
            )

            logger.info(f"Sent resend email to {recipient} for {send_date} "
                        f"({n} entries, thread={thread_id})")
        except Exception as e:
            logger.error(f"Failed to send resend to {recipient} for {send_date}: {e}")

    logger.info(f"Resend complete: {sent} emails sent ({len(candidates)} candidates)")


def run_resend(test_mode: bool = False, lookback_days: int = 7):
    """Run the daily-email re-send pass: detect changed days, send threaded updates."""
    db = get_db()
    send_resend_emails(db, test_mode=test_mode, lookback_days=lookback_days)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Timer Entries Review System")
    parser.add_argument("--send", action="store_true", help="Send daily timer entry emails")
    parser.add_argument("--apply", action="store_true", help="Process form responses + auto-resolve stale")
    parser.add_argument("--remind", action="store_true", help="Send duplicate reminder emails")
    parser.add_argument("--resend", action="store_true",
                        help="Re-send daily email (threaded) for days whose entry set changed")
    parser.add_argument("--resend-lookback-days", type=int, default=7,
                        help="How many days back to check for resend candidates (default 7)")
    parser.add_argument("--test", action="store_true", help="Test mode: send all emails to jamil only")
    parser.add_argument("--date", type=str, help="Target date YYYY-MM-DD (default: yesterday). For backfill sends.")
    args = parser.parse_args()

    if not any([args.send, args.apply, args.remind, args.resend]):
        parser.error("At least one of --send, --apply, --remind, --resend is required")

    target_date = None
    if args.date:
        from datetime import date as date_type
        target_date = date_type.fromisoformat(args.date)

    # Check OAuth token health before doing anything
    check_token_health()

    try:
        if args.apply:
            logger.info("=== Running --apply ===")
            run_apply(test_mode=args.test)

        if args.send:
            logger.info("=== Running --send ===")
            run_send(test_mode=args.test, target_date=target_date)

        if args.remind:
            logger.info("=== Running --remind ===")
            run_remind(test_mode=args.test)

        if args.resend:
            logger.info("=== Running --resend ===")
            run_resend(test_mode=args.test, lookback_days=args.resend_lookback_days)

    finally:
        close_db()


if __name__ == "__main__":
    main()
