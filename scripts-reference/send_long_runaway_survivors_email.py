"""
Follow-up audit email after the 2026-05-28 long-runaway-timer cleanup.

After bulk-removing all clean rows with duration_min > 14h (reason
'LONG_RUNAWAY_TIMER_AUDIT_2026-05-28'), some affected natural-key groups
still have a shorter sibling row in clean. Those survivors are what
shows up in this email. Each row carries Edit / Remove buttons so they
can be revised via the standard correction flow.

Recipient: jamil.mendez@ontel.co only.

Run:
    python send_long_runaway_survivors_email.py            # actually send
    python send_long_runaway_survivors_email.py --dry-run  # write HTML only
"""

import argparse
import asyncio
import base64
import os
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent / "swift_api_pipeline"
ENV_PATH = PIPELINE_DIR / ".env"
sys.path.insert(0, str(PIPELINE_DIR))

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
REMOVAL_REASON_PREFIX = "LONG_RUNAWAY_TIMER_AUDIT_2026-05-28"

# Survivors = clean rows whose (project_did, user_email, start_time, site, site_id, task)
# matches an entry that was just removed under the long-runaway reason.
SURVIVORS_QUERY = """
WITH affected AS (
    SELECT DISTINCT project_did, user_email, start_time,
                    site_name, site_id, task
    FROM app_timer.entry_removals
    WHERE reason LIKE $1 || '%'
)
SELECT
    c.project, c.project_did, c.user_email,
    c.site_name, c.site_id, c.task,
    c.start_time, c.end_time, c.duration_min
FROM affected a
JOIN data_staging.stg_timer_activities_clean c
  ON  c.project_did = a.project_did
  AND c.user_email  = a.user_email
  AND c.start_time  = a.start_time
  AND c.site_name IS NOT DISTINCT FROM a.site_name
  AND c.site_id   IS NOT DISTINCT FROM a.site_id
  AND c.task      IS NOT DISTINCT FROM a.task
ORDER BY c.duration_min DESC, c.user_email, c.start_time
"""

SUMMARY_QUERY = """
WITH affected AS (
    SELECT DISTINCT project_did, user_email, start_time,
                    site_name, site_id, task
    FROM app_timer.entry_removals
    WHERE reason LIKE $1 || '%'
),
groups_with_survivor AS (
    SELECT DISTINCT a.project_did, a.user_email, a.start_time,
                    a.site_name, a.site_id, a.task
    FROM affected a
    WHERE EXISTS (
        SELECT 1 FROM data_staging.stg_timer_activities_clean c
        WHERE c.project_did=a.project_did AND c.user_email=a.user_email
          AND c.start_time=a.start_time
          AND c.site_name IS NOT DISTINCT FROM a.site_name
          AND c.site_id   IS NOT DISTINCT FROM a.site_id
          AND c.task      IS NOT DISTINCT FROM a.task
    )
)
SELECT
    (SELECT COUNT(*) FROM app_timer.entry_removals
        WHERE reason LIKE $1 || '%')   AS total_removed,
    (SELECT COUNT(*) FROM affected)    AS affected_groups,
    (SELECT COUNT(*) FROM groups_with_survivor) AS groups_with_survivor,
    (SELECT COUNT(*) FROM affected) - (SELECT COUNT(*) FROM groups_with_survivor) AS groups_now_empty
"""


def _build_row(entry: dict) -> str:
    entry_id = _make_entry_id(
        entry["project_did"], entry["user_email"], entry["start_time"],
        entry.get("site_name"), entry.get("site_id"), entry.get("task"),
        entry.get("end_time"), entry.get("duration_min"),
    )
    project_raw = entry.get("project") or "(no project)"
    site_raw = entry.get("site_name") or "(no site)"
    task_raw = entry.get("task") or "(no task)"
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

    return f"""
        <tr>
            <td style="padding:6px 10px;border:1px solid #ddd;">{date}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{user}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{project}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{site}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{task}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{start}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">{end}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{duration}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;text-align:center;white-space:nowrap;">
                <a href="{correct_link}" style="display:inline-block;padding:5px 12px;
                   background:#1565c0;color:white;text-decoration:none;
                   border-radius:4px;font-size:11px;font-weight:bold;">Edit</a>
                <a href="{remove_link}" style="display:inline-block;padding:5px 12px;
                   background:#c62828;color:white;text-decoration:none;
                   border-radius:4px;font-size:11px;font-weight:bold;margin-left:4px;">Remove</a>
            </td>
        </tr>"""


def _build_html(survivors: list[dict], summary: dict) -> str:
    today = datetime.now(ET).strftime("%B %d, %Y")
    rows = "\n".join(_build_row(e) for e in survivors)
    return f"""
<html>
<body style="font-family:Arial,sans-serif;margin:0;padding:0;">
    <div style="background:#1565c0;color:white;padding:16px 24px;">
        <h2 style="margin:0;">Audit: Survivors After Long-Runaway-Timer Cleanup &mdash; {today}</h2>
    </div>
    <div style="padding:24px;">
        <p>Hi Jamil,</p>
        <p>The <strong>&gt;14h</strong> cleanup just landed. Summary:</p>
        <ul style="font-size:13px;line-height:1.7;">
            <li><strong>{summary['total_removed']:,}</strong> long-runaway entries removed from <code>stg_timer_activities_clean</code></li>
            <li><strong>{summary['affected_groups']:,}</strong> distinct (user / start / site / task) groups were affected</li>
            <li><strong>{summary['groups_with_survivor']:,}</strong> groups still have a shorter sibling kept in clean (listed below)</li>
            <li><strong>{summary['groups_now_empty']:,}</strong> groups are now empty in clean (the long entry was the only one for that natural key)</li>
        </ul>
        <p>The table below shows the <strong>{len(survivors):,}</strong> kept entries
           sorted by duration (longest first). Each row has Edit / Remove buttons
           if you want to adjust or pull any of these too.</p>

        <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Actions</h3>
        <ul style="font-size:13px;color:#555;margin:8px 0 16px;">
            <li><strong style="color:#1565c0;">Edit</strong> &mdash; fix a wrong duration</li>
            <li><strong style="color:#c62828;">Remove</strong> &mdash; delete the entry</li>
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
                    <th style="padding:8px 10px;border:1px solid #ddd;text-align:center;">Action</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>

        <div style="background:#f5f5f5;border-radius:6px;padding:14px 18px;margin-top:24px;font-size:13px;color:#555;">
            <p style="margin:0 0 8px;font-weight:bold;color:#333;">Notes</p>
            <ul style="margin:0;padding-left:20px;line-height:1.8;">
                <li>To undo a long-runaway removal, search
                    <code>app_timer.entry_removals</code> by entry_id and DELETE the
                    row (or set <code>reason='REVERTED'</code>), then rebuild
                    <code>stg_timer_activities_clean</code>.</li>
                <li>1 entry &gt;14h was NOT removed because it carries a tech
                    correction with reason "Forgot to start timer" (joanna.endriga
                    2026-02-23, 18.83h) &mdash; the tech explicitly entered that
                    duration via the form, so it is treated as valid.</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""


async def fetch_data() -> tuple[list[dict], dict]:
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
        survivors = await conn.fetch(SURVIVORS_QUERY, REMOVAL_REASON_PREFIX)
        summary_row = await conn.fetchrow(SUMMARY_QUERY, REMOVAL_REASON_PREFIX)
    finally:
        await conn.close()
    return [dict(r) for r in survivors], dict(summary_row)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    survivors, summary = asyncio.run(fetch_data())
    html = _build_html(survivors, summary)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    subject = (f"Audit: Survivors After Long-Runaway-Timer Cleanup - {today} "
               f"({len(survivors)} kept, {summary['total_removed']:,} removed)")

    if args.dry_run:
        out = Path.home() / "Desktop" / f"long_runaway_survivors_{today}.html"
        out.write_text(html, encoding="utf-8")
        print(f"Wrote dry-run HTML -> {out}")
        print(f"  Survivors: {len(survivors)} | Removed: {summary['total_removed']:,} "
              f"| Empty groups: {summary['groups_now_empty']:,}")
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
          f"msg={result.get('id')} | survivors={len(survivors)} "
          f"| removed={summary['total_removed']:,}")


if __name__ == "__main__":
    main()
