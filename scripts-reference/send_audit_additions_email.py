"""
One-off audit email: the 234 entries we added to stg_timer_activities_clean
during the 2026-05-28 duplicate-review audit.

Cohorts (by run_id in app_timer.entry_additions):
    00000000-0000-0000-0000-000000000003  -> 210 entries RESTORED from raw
                                              (longest-duration entry per
                                              empty, not-user-driven,
                                              no-correction group).
    00000000-0000-0000-0000-000000000004  ->  24 entries SYNTHESIZED from
                                              the duplicate-review JSONB
                                              snapshot (no matching row in
                                              stg_timer_activities).

Each entry is rendered with the same Edit / Remove buttons used in the
daily timer emails. For the 210 RESTORED entries the standard apply flow
will work because the underlying raw row still exists. For the 24
SYNTHESIZED entries the apply flow will NOT find a raw row to match
against -- those are flagged in the email so they can be handled
manually.

Recipient: jamil.mendez@ontel.co only.

Run:
    python send_audit_additions_email.py            # actually send
    python send_audit_additions_email.py --dry-run  # write HTML to disk only
"""

import argparse
import base64
import os
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import asyncio
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent / "swift_api_pipeline"
ENV_PATH = PIPELINE_DIR / ".env"
sys.path.insert(0, str(PIPELINE_DIR))

# Reuse helpers + form constants from timer_correction_review
from timer_correction_review import (  # noqa: E402
    _make_entry_id,
    _correct_form_url,
    _remove_form_url,
    _fmt_date,
    _fmt_time_short,
    _fmt_duration,
    _escape_html,
)

ET = ZoneInfo("America/New_York")
RECIPIENT = "jamil.mendez@ontel.co"

RESTORE_RUN_ID = "00000000-0000-0000-0000-000000000003"
SYNTH_RUN_ID   = "00000000-0000-0000-0000-000000000004"

QUERY = """
SELECT
    a.run_id::text             AS run_id,
    a.project,
    a.project_did,
    a.site_name,
    a.site_id,
    a.task,
    a.start_time,
    a.end_time,
    a.duration_min,
    a.user_email,
    a.added_at
FROM app_timer.entry_additions a
WHERE a.run_id IN ($1::uuid, $2::uuid)
ORDER BY a.start_time DESC NULLS LAST, a.user_email, a.added_at
"""


def _label(run_id: str) -> str:
    if run_id == RESTORE_RUN_ID:
        return "RESTORED"
    if run_id == SYNTH_RUN_ID:
        return "SYNTHESIZED"
    return "OTHER"


def _label_badge_html(run_id: str) -> str:
    if run_id == RESTORE_RUN_ID:
        return ('<span style="display:inline-block;background:#2e7d32;color:white;'
                'font-size:9px;padding:1px 6px;border-radius:3px;'
                'font-weight:bold;">RESTORED</span>')
    if run_id == SYNTH_RUN_ID:
        return ('<span style="display:inline-block;background:#6a1b9a;color:white;'
                'font-size:9px;padding:1px 6px;border-radius:3px;'
                'font-weight:bold;">SYNTHESIZED</span>')
    return ""


def _build_entries_html(entries: list[dict]) -> str:
    """Render entry rows. Same shape as the daily timer email, with a
    Type column (RESTORED / SYNTHESIZED) added between Duration and Action.
    """
    rows = []
    for entry in entries:
        entry_id = _make_entry_id(
            entry["project_did"], entry["user_email"], entry["start_time"],
            entry.get("site_name"), entry.get("site_id"), entry.get("task"),
            entry.get("end_time"), entry.get("duration_min"),
        )
        project_raw = entry.get("project") or "(no project)"
        site_raw    = entry.get("site_name") or "(no site)"
        task_raw    = entry.get("task") or "(no task)"
        project = _escape_html(project_raw)
        site    = _escape_html(site_raw)
        task    = _escape_html(task_raw)
        user    = _escape_html(entry.get("user_email") or "")
        date    = _escape_html(_fmt_date(entry["start_time"]))
        start   = _escape_html(_fmt_time_short(entry["start_time"]))
        end     = _escape_html(_fmt_time_short(entry.get("end_time")))
        duration = _escape_html(_fmt_duration(entry.get("duration_min")))

        details = (f"{project_raw} | {site_raw} | {task_raw} | "
                   f"{_fmt_date(entry['start_time'])} | "
                   f"{_fmt_duration(entry.get('duration_min'))}")
        correct_link = _correct_form_url(entry_id, details)
        remove_link  = _remove_form_url(entry_id, details)

        type_badge = _label_badge_html(entry["run_id"])

        # Note for SYNTHESIZED: the apply flow looks up entries in raw and
        # won't find these. Make the warning visible on hover via title.
        synth_note = ""
        if entry["run_id"] == SYNTH_RUN_ID:
            synth_note = (' <span title="The apply flow cannot find this entry '
                          'in stg_timer_activities. Handle manually." '
                          'style="color:#6a1b9a;font-weight:bold;cursor:help;">&#9432;</span>')

        rows.append(f"""
            <tr>
                <td style="padding:6px 10px;border:1px solid #ddd;">{date}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{user}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{project}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{site}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{task}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{start}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;">{end}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{duration}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;text-align:center;">{type_badge}{synth_note}</td>
                <td style="padding:6px 10px;border:1px solid #ddd;text-align:center;white-space:nowrap;">
                    <a href="{correct_link}" style="display:inline-block;padding:5px 12px;
                       background:#1565c0;color:white;text-decoration:none;
                       border-radius:4px;font-size:11px;font-weight:bold;">Edit</a>
                    <a href="{remove_link}" style="display:inline-block;padding:5px 12px;
                       background:#c62828;color:white;text-decoration:none;
                       border-radius:4px;font-size:11px;font-weight:bold;margin-left:4px;">Remove</a>
                </td>
            </tr>""")
    return "\n".join(rows)


def _build_html(entries: list[dict]) -> str:
    restored = sum(1 for e in entries if e["run_id"] == RESTORE_RUN_ID)
    synth    = sum(1 for e in entries if e["run_id"] == SYNTH_RUN_ID)
    total    = len(entries)
    today    = datetime.now(ET).strftime("%B %d, %Y")

    table_rows = _build_entries_html(entries)

    return f"""
<html>
<body style="font-family:Arial,sans-serif;margin:0;padding:0;">
    <div style="background:#1565c0;color:white;padding:16px 24px;">
        <h2 style="margin:0;">Audit: Timer Entries Added to Clean &mdash; {today}</h2>
    </div>
    <div style="padding:24px;">
        <p>Hi Jamil,</p>
        <p>Today's duplicate-review audit added <strong>{total}</strong>
           timer {'entry' if total == 1 else 'entries'} to
           <code>stg_timer_activities_clean</code> across two cohorts:</p>
        <ul style="font-size:13px;line-height:1.7;">
            <li><span style="display:inline-block;background:#2e7d32;color:white;
                font-size:10px;padding:1px 6px;border-radius:3px;font-weight:bold;">RESTORED</span>
                &nbsp;<strong>{restored}</strong> &mdash; longest-duration entry
                restored from <code>stg_timer_activities</code> for groups emptied by
                non-tech action with no correction.</li>
            <li><span style="display:inline-block;background:#6a1b9a;color:white;
                font-size:10px;padding:1px 6px;border-radius:3px;font-weight:bold;">SYNTHESIZED</span>
                &nbsp;<strong>{synth}</strong> &mdash; longest-duration entry
                reconstructed from the duplicate-review JSONB snapshot because
                no matching row exists in raw.</li>
        </ul>

        <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Actions</h3>
        <ul style="font-size:13px;color:#555;margin:8px 0 16px;">
            <li><strong style="color:#1565c0;">Edit</strong> &mdash; fix a wrong duration</li>
            <li><strong style="color:#c62828;">Remove</strong> &mdash; delete a duplicate or incorrect entry</li>
            <li>SYNTHESIZED entries don't exist in <code>stg_timer_activities</code>,
                so the standard apply flow won't find them.
                For those, reply to this email to request a manual fix.</li>
        </ul>

        <table style="border-collapse:collapse;width:100%;font-size:13px;margin:16px 0;">
            <thead>
                <tr style="background:#f5f5f5;">
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Date</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">User</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Project</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Site</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Task</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Start</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">End</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:left;">Duration</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:center;">Type</th>
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:center;">Action</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>

        <div style="background:#f5f5f5;border-radius:6px;padding:14px 18px;margin-top:24px;font-size:13px;color:#555;">
            <p style="margin:0 0 8px;font-weight:bold;color:#333;">Reference</p>
            <ul style="margin:0;padding-left:20px;line-height:1.8;">
                <li>RESTORED rows live in <code>app_timer.entry_additions</code>
                    with <code>run_id = {RESTORE_RUN_ID}</code> and reason starting
                    with <code>RESTORE_FROM_AUTO_REMOVAL</code>.</li>
                <li>SYNTHESIZED rows live in <code>app_timer.entry_additions</code>
                    with <code>run_id = {SYNTH_RUN_ID}</code> and reason starting
                    with <code>SYNTHESIZE_FROM_REVIEW_SNAPSHOT</code>.</li>
                <li>Both cohorts are inserted by section 3 of
                    <code>data_staging.rebuild_timer_clean()</code> and are not
                    filtered by <code>app_timer.entry_removals</code>.</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""


async def fetch_entries() -> list[dict]:
    load_dotenv(ENV_PATH)
    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
    )
    try:
        rows = await conn.fetch(QUERY, RESTORE_RUN_ID, SYNTH_RUN_ID)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Write HTML to disk and skip sending")
    args = parser.parse_args()

    entries = asyncio.run(fetch_entries())
    if not entries:
        print("No matching entries found in app_timer.entry_additions. Nothing to send.")
        return

    html = _build_html(entries)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    subject = f"Audit: Timer Entries Added to Clean - {today} ({len(entries)} entries)"

    if args.dry_run:
        out = Path.home() / "Desktop" / f"audit_additions_email_{today}.html"
        out.write_text(html, encoding="utf-8")
        print(f"Wrote dry-run HTML ({len(entries)} entries) -> {out}")
        return

    from gmail_client import authenticate
    service = authenticate()

    msg = MIMEMultipart()
    msg["To"]      = RECIPIENT
    msg["From"]    = "me"
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Sent to {RECIPIENT} | thread={result.get('threadId')} | "
          f"msg={result.get('id')} | entries={len(entries)}")


if __name__ == "__main__":
    main()
