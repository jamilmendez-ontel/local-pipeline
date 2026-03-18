#!/usr/bin/env python3
"""
Timer Discrepancies Review System

Detects duplicate timer entries (same user + start_time, different end_time/duration),
emails techs with a comparison table and pre-filled Google Form links to pick the
correct entry.  Reads form responses and rebuilds stg_timer_activities_clean
(deduplicated table) -- stg_timer_activities is never modified.

Supports N entries per group (not just 2). Tech picks ONE entry to keep;
all others are rejected. Labels: A, B, C, ... sorted by duration ascending.

Uses natural keys (project_did, user_email, start_time, end_time, duration_min)
instead of surrogate IDs because the timer pipeline DELETE+re-INSERTs the whole
month each run, which reassigns auto-increment IDs nightly.

Modes:
    --notify   Detect new duplicates for current month, email each tech
    --resolve  Read Google Sheet responses + auto-resolve stale entries (>7 days)
    --remind   Resend emails for unresolved groups older than 1 day

Usage:
    python timer_duplicate_review.py --notify              # detect + email
    python timer_duplicate_review.py --resolve             # process responses + auto-resolve
    python timer_duplicate_review.py --remind              # reminder emails
    python timer_duplicate_review.py --notify --test       # send only to jamil
    python timer_duplicate_review.py --resolve --notify    # both in one run
"""

import argparse
import base64
import hashlib
import json
import string
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import SCHEMA_STAGING, get_logger, get_db, close_db, retry_db, setup_logging

logger = get_logger("timer_dup_review")

TZ_EASTERN = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------
# Google Form configuration (jamil.mendez@ontel.co)
# --------------------------------------------------------------------------
GOOGLE_FORM_ID = "1FAIpQLSdRffnutXnfbjNUvqQSh6icSuekoeSGwXhtiVk7trmgm1Cv6A"
FORM_ENTRY_GROUP_ID = "entry.374636745"    # Field ID for "Group ID"
FORM_ENTRY_SELECTION = "entry.1928021717"  # Field ID for "Selection"
RESPONSE_SHEET_ID = "1e5W7613Rp41CWpLQnjwqFwSKdJnv9C51kSKeNWDkGT8"

# Auto-resolve after this many days with no response
AUTO_RESOLVE_DAYS = 7
# Send reminder after this many days
REMINDER_AFTER_DAYS = 1

# Entry labels: A, B, C, ... Z (max 26 entries per group)
LABELS = list(string.ascii_uppercase)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _make_group_id(project_did: str, user_email: str, start_time: datetime,
                   site_name: str = None, site_id: str = None, task: str = None) -> str:
    """Create a 12-char hex group ID from the duplicate key."""
    raw = f"{project_did}|{user_email}|{start_time.isoformat()}|{site_name}|{site_id}|{task}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _form_url(group_id: str, selection: str) -> str:
    """Build a pre-filled Google Form URL."""
    base = f"https://docs.google.com/forms/d/e/{GOOGLE_FORM_ID}/viewform"
    return f"{base}?{FORM_ENTRY_GROUP_ID}={group_id}&{FORM_ENTRY_SELECTION}={selection}&submit=Submit"


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


def _entries_to_jsonb(entries: list[dict]) -> list[dict]:
    """Prepare entries list for asyncpg JSONB column (pass as Python list, not string).

    asyncpg's JSONB codec handles json.dumps internally, so we just need to
    ensure all values are JSON-serializable (no datetime objects).
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


# --------------------------------------------------------------------------
# --notify: Detect duplicates and email techs
# --------------------------------------------------------------------------

def detect_duplicates(db) -> list[dict]:
    """Find duplicate groups in stg_timer_activities for current month.

    Returns list of dicts with group info and all entries (sorted by duration).
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    rows = retry_db(
        lambda: db.fetch(f"""
            WITH dupes AS (
                SELECT project_did, user_email, start_time, site_name, site_id, task,
                       COUNT(*) as cnt
                FROM {SCHEMA_STAGING}.stg_timer_activities
                WHERE start_time >= $1
                GROUP BY project_did, user_email, start_time, site_name, site_id, task
                HAVING COUNT(*) > 1
            )
            SELECT t.project_did, t.project, t.user_email, t.start_time,
                   t.end_time, t.duration_min, t.site_name, t.site_id, t.task
            FROM {SCHEMA_STAGING}.stg_timer_activities t
            JOIN dupes d ON t.project_did = d.project_did
                        AND t.user_email = d.user_email
                        AND t.start_time = d.start_time
                        AND t.site_name IS NOT DISTINCT FROM d.site_name
                        AND t.site_id   IS NOT DISTINCT FROM d.site_id
                        AND t.task      IS NOT DISTINCT FROM d.task
            ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.task, t.duration_min
        """, month_start),
        description="detect timer duplicates",
    )

    if not rows:
        return []

    # Group rows by (project_did, user_email, start_time, site_name, site_id, task)
    groups = {}
    for row in rows:
        key = (row["project_did"], row["user_email"], row["start_time"],
               row["site_name"], row["site_id"], row["task"])
        groups.setdefault(key, []).append(row)

    # For each group, strip NULL end_time entries (still-running snapshots),
    # then label remaining A, B, C, ... sorted by duration ascending.
    # If only 1 entry remains after stripping, it's no longer a duplicate — skip.
    results = []
    null_stripped = 0
    for (project_did, user_email, start_time, site_name, site_id, task), raw_entries in groups.items():
        # Filter out entries with NULL end_time (timer still running when API snapshot taken)
        completed_entries = [r for r in raw_entries if r["end_time"] is not None]
        null_stripped += len(raw_entries) - len(completed_entries)

        if len(completed_entries) < 2:
            continue  # No longer a duplicate after stripping NULLs

        sorted_entries = sorted(completed_entries, key=lambda r: float(r["duration_min"] or 0))

        entries = []
        for i, e in enumerate(sorted_entries):
            if i >= len(LABELS):
                break  # Cap at 26
            entries.append({
                "label": LABELS[i],
                "end_time": e["end_time"],
                "duration_min": e["duration_min"],
            })

        group_id = _make_group_id(project_did, user_email, start_time, site_name, site_id, task)
        results.append({
            "group_id": group_id,
            "project_did": project_did,
            "project": completed_entries[0]["project"],
            "user_email": user_email,
            "start_time": start_time,
            "site_name": site_name,
            "site_id": site_id,
            "task": task,
            "entries": entries,
        })

    if null_stripped:
        logger.info(f"Stripped {null_stripped} NULL end_time entries (still-running timers)")

    return results


def filter_new_groups(db, groups: list[dict]) -> list[dict]:
    """Filter out groups already tracked in the review table."""
    if not groups:
        return []

    group_ids = [g["group_id"] for g in groups]
    existing = retry_db(
        lambda: db.fetch(
            f"SELECT group_id FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
            f"WHERE group_id = ANY($1)",
            group_ids,
        ),
        description="check existing review groups",
    )
    existing_ids = {row["group_id"] for row in existing}
    return [g for g in groups if g["group_id"] not in existing_ids]


def store_review_records(db, groups: list[dict]):
    """Insert new review records with status='notified'."""
    now = datetime.now(timezone.utc)
    for g in groups:
        entries_json = _entries_to_jsonb(g["entries"])
        retry_db(
            lambda g=g, ej=entries_json: db.execute(
                f"INSERT INTO {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
                f"(group_id, project_did, project, user_email, start_time, "
                f" site_name, site_id, task, entries, status, notified_at) "
                f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                g["group_id"], g["project_did"], g["project"], g["user_email"],
                g["start_time"], g["site_name"], g["site_id"], g["task"],
                ej, "notified", now,
            ),
            description=f"insert review {g['group_id']}",
        )
    logger.info(f"Stored {len(groups)} review records")


def _build_comparison_html(groups: list[dict]) -> str:
    """Build HTML comparison tables for one tech's duplicate groups."""
    sections = []
    for g in groups:
        entries = g["entries"]
        gid = g["group_id"]
        n_entries = len(entries)

        # Build table header row
        header_cells = '<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;width:15%;">&nbsp;</th>'
        col_width = 85 // n_entries
        for e in entries:
            header_cells += (
                f'<th style="padding:8px 12px;border:1px solid #ddd;text-align:left;width:{col_width}%;">'
                f'Entry {e["label"]}</th>'
            )

        # Data rows — site/task are shared per group, only end_time and duration differ
        def _row(label, key, fmt_fn=None):
            cells = f'<td style="padding:6px 12px;border:1px solid #ddd;font-weight:bold;">{label}</td>'
            for e in entries:
                val = e.get(key)
                display = fmt_fn(val) if fmt_fn else (val or "-")
                cells += f'<td style="padding:6px 12px;border:1px solid #ddd;">{display}</td>'
            return f"<tr>{cells}</tr>"

        rows_html = "\n".join([
            _row("End Time", "end_time", _fmt_time),
            _row("Duration", "duration_min", _fmt_duration),
        ])

        # Build buttons
        buttons = []
        for e in entries:
            label = e["label"]
            link = _form_url(gid, label)
            buttons.append(
                f'<a href="{link}" style="display:inline-block;padding:10px 24px;'
                f'background:#1976d2;color:white;text-decoration:none;border-radius:4px;'
                f'margin:4px 6px;font-weight:bold;">Select Entry {label}</a>'
            )
        buttons_html = "\n".join(buttons)

        site_display = g.get("site_name") or "(no site)"
        task_display = g.get("task") or "(no task)"
        site_id_display = g.get("site_id") or ""
        start_display = _fmt_time(g["start_time"])

        section = f"""
        <div style="margin-bottom:24px;border:1px solid #ddd;border-radius:6px;overflow:hidden;">
            <div style="background:#f5f5f5;padding:10px 16px;">
                <div style="font-weight:bold;">{g['project']} &mdash; {site_display} &mdash; {task_display}</div>
                <div style="color:#555;font-size:13px;margin-top:4px;">
                    Site ID: {site_id_display or '-'} &middot; Start: {start_display}
                    <span style="color:#888;margin-left:12px;">ID: {gid} &middot; {n_entries} entries</span>
                </div>
            </div>
            <table style="border-collapse:collapse;width:100%;font-size:14px;">
                <thead>
                    <tr style="background:#fafafa;">
                        {header_cells}
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            <div style="padding:12px 16px;text-align:center;">
                {buttons_html}
            </div>
        </div>"""
        sections.append(section)

    return "\n".join(sections)


def send_review_emails(db, groups: list[dict], test_mode: bool = False):
    """Send one email per tech with all their duplicate groups.

    Stores the Gmail thread ID on review records so reminders can reply
    in the same thread.

    Args:
        db: Database instance.
        test_mode: If True, send all emails to jamil only.
    """
    from gmail_client import authenticate

    # Group by user_email
    by_user = {}
    for g in groups:
        by_user.setdefault(g["user_email"], []).append(g)

    service = authenticate()

    for user_email, user_groups in by_user.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(user_groups)
        subject = f"Timer Discrepancies Review - {n} {'entry needs' if n == 1 else 'entries need'} your input"

        comparisons_html = _build_comparison_html(user_groups)

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#1976d2;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Discrepancies Review</h2>
            </div>
            <div style="padding:24px;">
                <p>Hi,</p>
                <p>We found <strong>{n}</strong> timer {'entry' if n == 1 else 'entries'} with duplicate
                   start times that need your review. For each one below, please click the button
                   for the entry you want to <strong>keep</strong>. All others will be removed.</p>

                {comparisons_html}

                <p style="color:#888;font-size:13px;margin-top:24px;">
                    If no selection is made within 7 days, the entry with the latest end time will
                    be kept automatically.
                </p>
            </div>
        </body>
        </html>
        """

        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            thread_id = result.get("threadId")
            gmail_msg_id = result.get("id")

            # Fetch the sent message to get the RFC Message-ID header for threading
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

            logger.info(f"Sent review email to {recipient} ({n} groups, thread={thread_id})")

            # Store thread ID and Message-ID so reminders can reply in same thread
            if thread_id:
                group_ids = [g["group_id"] for g in user_groups]
                retry_db(
                    lambda gids=group_ids, tid=thread_id, mid=message_id: db.execute(
                        f"UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
                        f"SET notification_thread_id = $1, notification_message_id = $2, "
                        f"    updated_at = NOW() "
                        f"WHERE group_id = ANY($3) AND notification_thread_id IS NULL",
                        tid, mid, gids,
                    ),
                    description=f"store thread_id for {user_email}",
                )
        except Exception as e:
            logger.error(f"Failed to send review email to {recipient}: {e}")


def run_notify(test_mode: bool = False):
    """Detect new duplicates and email techs."""
    db = get_db()

    logger.info("Detecting duplicate timer entries...")
    all_groups = detect_duplicates(db)
    logger.info(f"Found {len(all_groups)} duplicate groups total")

    if not all_groups:
        logger.info("No duplicates found")
        return

    new_groups = filter_new_groups(db, all_groups)
    logger.info(f"New groups (not yet tracked): {len(new_groups)}")

    if not new_groups:
        logger.info("All duplicate groups already tracked")
        return

    store_review_records(db, new_groups)
    send_review_emails(db, new_groups, test_mode=test_mode)
    logger.info(f"Notify complete: {len(new_groups)} groups emailed")


# --------------------------------------------------------------------------
# --resolve: Read form responses + auto-resolve stale entries
# --------------------------------------------------------------------------

def read_form_responses() -> list[dict]:
    """Read Google Sheet responses and return parsed rows."""
    from sheets_client import authenticate_sheets, read_spreadsheet

    logger.info("Reading form responses from Google Sheet...")
    creds = authenticate_sheets()
    rows = read_spreadsheet(creds, RESPONSE_SHEET_ID)

    if len(rows) <= 1:
        logger.info("No form responses found")
        return []

    headers = [h.strip().lower() for h in rows[0]]
    # Dedup by group_id — last response wins (tech may correct their choice)
    by_group = {}
    for row in rows[1:]:
        row_dict = {}
        for i, val in enumerate(row):
            if i < len(headers):
                row_dict[headers[i]] = val.strip()
        group_id = row_dict.get("group id", "").strip()
        selection = row_dict.get("selection", "").strip().upper()
        if group_id and len(selection) == 1 and selection in LABELS:
            by_group[group_id] = selection  # overwrites earlier responses

    results = [{"group_id": gid, "selection": sel} for gid, sel in by_group.items()]
    logger.info(f"Parsed {len(results)} valid form responses (deduped by group_id, last wins)")
    return results


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


def _get_rejected_entries(entries_json, selection: str) -> list[dict]:
    """Given all entries and the selected label, return list of rejected natural keys."""
    entries = entries_json if isinstance(entries_json, list) else json.loads(entries_json)
    rejected = []
    for e in entries:
        if e["label"] != selection:
            rejected.append({
                "end_time": e.get("end_time"),
                "duration_min": e.get("duration_min"),
            })
    return rejected


def resolve_from_responses(db, responses: list[dict]):
    """Process form responses: store rejected natural keys, rebuild clean table."""
    now = datetime.now(timezone.utc)
    resolved_any = False

    for resp in responses:
        group_id = resp["group_id"]
        selection = resp["selection"]

        review = retry_db(
            lambda gid=group_id: db.fetchrow(
                f"SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
                f"WHERE group_id = $1", gid,
            ),
            description=f"lookup review {group_id}",
        )

        if not review:
            logger.warning(f"No review record for group_id={group_id}, skipping")
            continue

        # If already resolved by tech with the same selection, skip
        if review["status"] == "resolved" and review["resolved_by"] == "tech" and review["selected_entry"] == selection:
            logger.info(f"Group {group_id} already resolved by tech with same selection {selection}, skipping")
            continue

        # Validate selection exists in entries
        entries = review["entries"] if isinstance(review["entries"], list) else json.loads(review["entries"])
        valid_labels = {e["label"] for e in entries}
        if selection not in valid_labels:
            logger.warning(f"Group {group_id}: selection '{selection}' not in entries {valid_labels}, skipping")
            continue

        # Build rejected entries list (all except selected)
        rejected = _get_rejected_entries(entries, selection)

        was_auto = review["status"] == "auto_resolved"
        if was_auto:
            logger.info(f"Group {group_id} was auto-resolved, overriding with tech selection")

        retry_db(
            lambda gid=group_id, sel=selection, rej=rejected: db.execute(
                f"UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
                f"SET status = 'resolved', selected_entry = $1, "
                f"    rejected_entries = $2, "
                f"    resolved_at = $3, resolved_by = 'tech', updated_at = $3 "
                f"WHERE group_id = $4",
                sel, rej, now, gid,
            ),
            description=f"resolve review {group_id}",
        )
        logger.info(f"Resolved group {group_id}: kept {selection}, rejected {len(rejected)} entries")
        resolved_any = True

    return resolved_any


def auto_resolve_stale(db):
    """Auto-resolve entries older than AUTO_RESOLVE_DAYS: keep latest end_time."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=AUTO_RESOLVE_DAYS)

    stale = retry_db(
        lambda: db.fetch(
            f"SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
            f"WHERE status IN ('pending', 'notified') "
            f"  AND notified_at IS NOT NULL AND notified_at < $1",
            cutoff,
        ),
        description="find stale reviews",
    )

    if not stale:
        logger.info("No stale reviews to auto-resolve")
        return False

    logger.info(f"Auto-resolving {len(stale)} stale reviews (>{AUTO_RESOLVE_DAYS} days)...")

    for review in stale:
        entries = review["entries"] if isinstance(review["entries"], list) else json.loads(review["entries"])

        # Keep the entry with the latest end_time
        best = max(entries, key=lambda e: e.get("end_time") or "")
        selection = best["label"]

        rejected = _get_rejected_entries(entries, selection)

        retry_db(
            lambda gid=review["group_id"], sel=selection, rej=rejected: db.execute(
                f"UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
                f"SET status = 'auto_resolved', selected_entry = $1, "
                f"    rejected_entries = $2, "
                f"    resolved_at = $3, resolved_by = 'auto', updated_at = $3 "
                f"WHERE group_id = $4",
                sel, rej, now, gid,
            ),
            description=f"auto-resolve update {review['group_id']}",
        )
        logger.info(f"Auto-resolved group {review['group_id']}: kept {selection}, rejected {len(rejected)} entries")

    return True


def run_resolve():
    """Process form responses, auto-resolve stale entries, rebuild clean table."""
    db = get_db()

    # 1. Process form responses
    responses = read_form_responses()
    if responses:
        resolve_from_responses(db, responses)
        logger.info(f"Processed {len(responses)} form responses")

    # 2. Auto-resolve stale entries
    auto_resolve_stale(db)

    # 3. Rebuild clean table (always -- picks up new staging data too)
    rebuild_clean_table(db)


# --------------------------------------------------------------------------
# --remind: Send reminder emails for unresolved groups
# --------------------------------------------------------------------------

def run_remind(test_mode: bool = False):
    """Send daily reminder emails for all unresolved groups."""
    db = get_db()
    now = datetime.now(timezone.utc)

    unresolved = retry_db(
        lambda: db.fetch(
            f"SELECT * FROM {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
            f"WHERE status IN ('pending', 'notified') "
            f"  AND notified_at IS NOT NULL",
        ),
        description="find unresolved reviews for reminder",
    )

    if not unresolved:
        logger.info("No unresolved reviews need reminders")
        return

    # Rebuild group dicts for email template
    groups = []
    for r in unresolved:
        entries = r["entries"] if isinstance(r["entries"], list) else json.loads(r["entries"])
        days_pending = (now - r["notified_at"]).days
        groups.append({
            "group_id": r["group_id"],
            "project_did": r["project_did"],
            "project": r["project"],
            "user_email": r["user_email"],
            "start_time": r["start_time"],
            "site_name": r["site_name"],
            "site_id": r["site_id"],
            "task": r["task"],
            "entries": entries,
            "days_pending": days_pending,
            "notification_thread_id": r.get("notification_thread_id"),
            "notification_message_id": r.get("notification_message_id"),
        })

    # Group by user and send reminder emails (as replies in same thread)
    from gmail_client import authenticate

    by_user = {}
    for g in groups:
        by_user.setdefault(g["user_email"], []).append(g)

    service = authenticate()

    for user_email, user_groups in by_user.items():
        recipient = "jamil.mendez@ontel.co" if test_mode else user_email
        n = len(user_groups)
        max_days = max(g["days_pending"] for g in user_groups)

        # Build a simple summary list (no buttons — tech should reply from original email)
        summary_items = []
        for g in user_groups:
            site = g.get("site_name") or "(no site)"
            task = g.get("task") or "(no task)"
            days = g["days_pending"]
            days_label = f"{days} day{'s' if days != 1 else ''}"
            summary_items.append(
                f'<li>{g["project"]} &mdash; {site} &mdash; {task} '
                f'<span style="color:#c62828;">({days_label})</span> '
                f'<span style="color:#888;">(ID: {g["group_id"]})</span></li>'
            )
        summary_html = "\n".join(summary_items)

        days_left = max(0, AUTO_RESOLVE_DAYS - max_days)
        auto_resolve_warning = (
            f"Entries will be auto-resolved in <strong>{days_left} day{'s' if days_left != 1 else ''}</strong> "
            f"(latest end time kept)."
            if days_left > 0
            else "Entries will be <strong>auto-resolved today</strong> (latest end time kept)."
        )

        html_body = f"""
        <html>
        <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
            <div style="background:#e65100;color:white;padding:16px 24px;">
                <h2 style="margin:0;">Timer Discrepancies Review - Reminder ({max_days} day{'s' if max_days != 1 else ''} pending)</h2>
            </div>
            <div style="padding:24px;">
                <p>Hi,</p>
                <p>This is a reminder that <strong>{n}</strong> timer duplicate
                   {'review is' if n == 1 else 'reviews are'} still pending.
                   Please go back to the original review email and select the correct entry.</p>

                <p><strong>Pending reviews:</strong></p>
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

        # Find thread ID and Message-ID from any of this user's groups
        thread_id = None
        orig_message_id = None
        for g in user_groups:
            if g.get("notification_thread_id"):
                thread_id = g["notification_thread_id"]
                orig_message_id = g.get("notification_message_id")
                break

        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        # "Re:" prefix so Gmail threads it as a reply
        msg["Subject"] = f"Re: Timer Discrepancies Review - {n} {'entry needs' if n == 1 else 'entries need'} your input ({max_days} day{'s' if max_days != 1 else ''} not reviewed)"
        # Set reply headers so the reminder shows under the original in the thread
        if orig_message_id:
            msg["In-Reply-To"] = orig_message_id
            msg["References"] = orig_message_id
        msg.attach(MIMEText(html_body, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
            send_body = {"raw": raw}
            if thread_id:
                send_body["threadId"] = thread_id
            service.users().messages().send(userId="me", body=send_body).execute()
            logger.info(f"Sent reminder to {recipient} ({n} groups, thread={thread_id})")
        except Exception as e:
            logger.error(f"Failed to send reminder to {user_email}: {e}")

    # Update reminder counts
    group_ids = [g["group_id"] for g in groups]
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_STAGING}.stg_timer_duplicate_reviews "
            f"SET reminder_count = reminder_count + 1, last_reminder_at = $1, updated_at = $1 "
            f"WHERE group_id = ANY($2)",
            now, group_ids,
        ),
        description="update reminder counts",
    )
    logger.info(f"Remind complete: sent reminders for {len(groups)} groups")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Timer Discrepancies Review System")
    parser.add_argument("--notify", action="store_true", help="Detect new duplicates and email techs")
    parser.add_argument("--resolve", action="store_true", help="Process form responses + auto-resolve stale")
    parser.add_argument("--remind", action="store_true", help="Send reminder emails for unresolved groups")
    parser.add_argument("--test", action="store_true", help="Test mode: send all emails to jamil only")
    args = parser.parse_args()

    if not any([args.notify, args.resolve, args.remind]):
        parser.error("At least one of --notify, --resolve, --remind is required")

    try:
        if args.resolve:
            logger.info("=== Running --resolve ===")
            run_resolve()

        if args.notify:
            logger.info("=== Running --notify ===")
            run_notify(test_mode=args.test)

        if args.remind:
            logger.info("=== Running --remind ===")
            run_remind(test_mode=args.test)

    finally:
        close_db()


if __name__ == "__main__":
    main()
