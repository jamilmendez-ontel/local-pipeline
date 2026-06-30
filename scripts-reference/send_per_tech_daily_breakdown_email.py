"""
One-off email: per-tech daily timer breakdowns for techs flagged with
unusually high daily totals.

Targets (hardcoded for the 2026-05-28 audit):
    Ronald Paul Nieva  -- 2026-04-29  (~33.3h total)
    Joanna Endriga     -- 2026-02-23  (~31.5h total)
    Mike Silvosa       -- 2026-05-05  (~28.9h total)
    James Montalbo     -- 2026-02-03  (~24.5h total)

One section per tech, all entries from that ET day, each row with
Edit / Remove buttons (same form URLs as the daily timer email).

Recipient: jamil.mendez@ontel.co only.

Run:
    python send_per_tech_daily_breakdown_email.py            # send
    python send_per_tech_daily_breakdown_email.py --dry-run  # write HTML only
"""

import argparse
import asyncio
import base64
import os
import sys
from datetime import date, datetime
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

TARGETS = [
    ("Ronald Paul Nieva", "ronald.nieva@ontel.co",  date(2026, 4, 29)),
    ("Joanna Endriga",    "joanna.endriga@ontel.co", date(2026, 2, 23)),
    ("Mike Silvosa",      "mike.silvosa@ontel.co",   date(2026, 5,  5)),
    ("James Montalbo",    "james@ontel.co",          date(2026, 2,  3)),
]

DAY_QUERY = """
SELECT
    project, project_did, user_email,
    site_name, site_id, task,
    start_time, end_time, duration_min
FROM data_staging.stg_timer_activities_clean
WHERE user_email = $1
  AND (start_time AT TIME ZONE 'America/New_York')::date = $2
ORDER BY start_time
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
    start = _escape_html(_fmt_time_short(entry["start_time"]))
    end   = _escape_html(_fmt_time_short(entry.get("end_time")))
    duration = _escape_html(_fmt_duration(entry.get("duration_min")))

    details = (f"{project_raw} | {site_raw} | {task_raw} | "
               f"{_fmt_date(entry['start_time'])} | "
               f"{_fmt_duration(entry.get('duration_min'))}")
    correct_link = _correct_form_url(entry_id, details)
    remove_link  = _remove_form_url(entry_id, details)

    return f"""
        <tr>
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


def _build_section(name: str, email: str, entry_date: date, entries: list[dict]) -> str:
    total_min = sum(float(e["duration_min"] or 0) for e in entries)
    total_hr  = total_min / 60
    date_str  = entry_date.strftime("%B %d, %Y")

    if not entries:
        return f"""
        <h3 style="margin-top:24px;margin-bottom:4px;color:#1565c0;">
            {_escape_html(name)} &mdash; {date_str}
        </h3>
        <p style="font-size:13px;color:#888;margin:0 0 16px;">
            No entries in clean for {_escape_html(email)} on this date.
        </p>"""

    rows = "\n".join(_build_row(e) for e in entries)
    return f"""
        <h3 style="margin-top:24px;margin-bottom:4px;color:#1565c0;">
            {_escape_html(name)} &mdash; {date_str}
        </h3>
        <p style="font-size:13px;color:#555;margin:0 0 10px;">
            {_escape_html(email)} &nbsp;|&nbsp; <strong>{len(entries)}</strong> entries
            &nbsp;|&nbsp; total <strong>{_fmt_duration(total_min)}</strong> ({total_hr:.2f}h)
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px;">
            <thead>
                <tr style="background:#f5f5f5;">
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
        </table>"""


def _build_html(sections_html: str, overall_totals: list[tuple]) -> str:
    today = datetime.now(ET).strftime("%B %d, %Y")
    summary_rows = "\n".join(
        f"<li><strong>{_escape_html(n)}</strong> &mdash; {d.strftime('%b %d, %Y')}: "
        f"{nent} entries, {h:.2f}h total</li>"
        for (n, d, nent, h) in overall_totals
    )
    return f"""
<html>
<body style="font-family:Arial,sans-serif;margin:0;padding:0;">
    <div style="background:#1565c0;color:white;padding:16px 24px;">
        <h2 style="margin:0;">Per-Tech Daily Timer Breakdowns &mdash; {today}</h2>
    </div>
    <div style="padding:24px;">
        <p>Hi Jamil,</p>
        <p>Daily timer breakdown for the four techs you flagged with unusually
           high daily totals. Each row carries Edit / Remove buttons in case
           anything needs to be revised or pulled.</p>

        <h3 style="margin-top:8px;margin-bottom:4px;font-size:15px;">Summary</h3>
        <ul style="font-size:13px;line-height:1.7;">
            {summary_rows}
        </ul>

        <h3 style="margin-top:20px;margin-bottom:8px;font-size:15px;">Actions</h3>
        <ul style="font-size:13px;color:#555;margin:8px 0 16px;">
            <li><strong style="color:#1565c0;">Edit</strong> &mdash; fix a wrong duration</li>
            <li><strong style="color:#c62828;">Remove</strong> &mdash; delete the entry</li>
        </ul>
        {sections_html}
    </div>
</body>
</html>
"""


async def fetch_all() -> tuple[str, list[tuple]]:
    load_dotenv(ENV_PATH)
    conn = await asyncpg.connect(
        host=os.getenv("SUPABASE_HOST"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        database=os.getenv("SUPABASE_DB"),
        user=os.getenv("SUPABASE_USER"),
        password=os.getenv("SUPABASE_PASSWORD"),
        ssl="require",
    )
    sections = []
    totals = []
    try:
        for name, email, d in TARGETS:
            rows = await conn.fetch(DAY_QUERY, email, d)
            entries = [dict(r) for r in rows]
            sections.append(_build_section(name, email, d, entries))
            total_h = sum(float(e["duration_min"] or 0) for e in entries) / 60
            totals.append((name, d, len(entries), total_h))
    finally:
        await conn.close()
    return "\n".join(sections), totals


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    sections_html, totals = asyncio.run(fetch_all())
    html = _build_html(sections_html, totals)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    subject = f"Per-Tech Daily Timer Breakdowns - {today}"

    if args.dry_run:
        out = Path.home() / "Desktop" / f"per_tech_daily_breakdown_{today}.html"
        out.write_text(html, encoding="utf-8")
        print(f"Wrote dry-run HTML -> {out}")
        for n, d, nent, h in totals:
            print(f"  {n} {d}: {nent} entries / {h:.2f}h")
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
          f"msg={result.get('id')}")
    for n, d, nent, h in totals:
        print(f"  {n} {d}: {nent} entries / {h:.2f}h")


if __name__ == "__main__":
    main()
