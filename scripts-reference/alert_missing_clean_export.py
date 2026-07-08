#!/usr/bin/env python3
"""
Guard: alert if the daily Timer Clean Data Export did not go out.

The clean-export and corrections-apply steps in pipeline-timer.yml run with
continue-on-error, so a transient failure (e.g. a Supabase session-mode pooler
cap, EMAXCONNSESSION) used to skip the export silently while the run still went
green. This guard runs at the end of the job and turns that silent skip into a
loud one: it emails an alert and exits non-zero so the run shows red.

It flags a problem when EITHER
  - the clean-export step did not succeed (--step-outcome != success), OR
  - no export file for today's ET date exists on disk.

Usage (from scripts-reference/):
    python alert_missing_clean_export.py --step-outcome "$OUTCOME"

Exit code 0 = export looks healthy; 1 = alerted (missing/failed).
"""

import argparse
import base64
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = SCRIPT_DIR.parent / "swift_api_pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

ET = ZoneInfo("America/New_York")
EXPORT_DIR = SCRIPT_DIR / "data_sample" / "timer_clean_exports"

# Ops alert goes to the owner who can act on it, not the whole report list.
ALERT_RECIPIENTS = ["jamil.mendez@ontel.co"]


def todays_export_exists():
    """True if a TimerCleanData_*_<today-ET>.xlsx file is present."""
    today_et = datetime.now(ET).strftime("%Y%m%d")
    if not EXPORT_DIR.exists():
        return False, today_et
    matches = list(EXPORT_DIR.glob(f"TimerCleanData_*_{today_et}.xlsx"))
    return bool(matches), today_et


def send_alert(step_outcome, today_et):
    from gmail_client import authenticate

    service = authenticate()
    subject = f"[ALERT] Timer Clean Data Export did NOT send - {datetime.now(ET):%B %d, %Y}"
    html_body = f"""\
    <html><body style="font-family: Arial, sans-serif;">
    <h2 style="color:#c62828;">Timer Clean Data Export did not go out</h2>
    <p>The daily <strong>Timer Clean Data Export</strong> was not delivered today.
    This alert exists because the export step runs with continue-on-error, so a
    failure would otherwise leave the pipeline run green.</p>
    <table style="border-collapse: collapse; margin: 16px 0; border: 1px solid #ddd;">
        <tr style="background:#f5f5f5;"><td style="padding:4px 12px;">Clean-export step outcome</td>
            <td style="padding:4px 12px;"><strong>{step_outcome}</strong></td></tr>
        <tr><td style="padding:4px 12px;">Export file for {today_et}</td>
            <td style="padding:4px 12px;"><strong>not found</strong></td></tr>
    </table>
    <p>Most common cause is a transient Supabase pooler cap (EMAXCONNSESSION).
    To recover, run the <strong>Timer: Clean Export (manual)</strong> workflow
    (workflow_dispatch) - it reruns only the clean-export path and emails the
    internal recipients without touching the tech daily emails.</p>
    </body></html>"""

    msg = MIMEText(html_body, "html")
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg["From"] = "me"
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Alert email sent to {', '.join(ALERT_RECIPIENTS)}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Alert if the clean export did not send")
    parser.add_argument(
        "--step-outcome",
        default="unknown",
        help="Outcome of the clean-export workflow step (success/failure/...)",
    )
    args = parser.parse_args()

    exists, today_et = todays_export_exists()
    if args.step_outcome == "success" and exists:
        print(f"Clean export healthy: step succeeded and file for {today_et} is present.")
        return 0

    print(
        f"Clean export PROBLEM: step outcome={args.step_outcome}, "
        f"file for {today_et} present={exists}. Sending alert."
    )
    try:
        send_alert(args.step_outcome, today_et)
    except Exception as e:  # noqa: BLE001 - never let the alert itself mask the failure
        print(f"WARNING: failed to send alert email: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
