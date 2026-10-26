#!/usr/bin/env python3
"""Dispatch cadence watcher: alert when repository_dispatch runs stop arriving.

Why (OnPoint INIT-827): on 2026-09-30 the GitHub PAT that the Apps Script
triggers use for repository_dispatch expired. Every dispatch-driven pipeline
stopped for 22 hours and nothing alerted: pipeline_health_watcher.py checks
pg_cron, MV freshness and row counts, never whether dispatches still arrive.

What it checks (GitHub REST API, built-in GITHUB_TOKEN with actions: read, so
the check never depends on the PAT it is watching):
  1. any_dispatch: the newest repository_dispatch run of ANY workflow here.
  2. One heartbeat per Apps Script project that fires every 5 minutes, so a
     token that dies in only one project is caught too:
       - Pipeline: Daily Reports (Rolling)  <- ontel account, daily_reports_trigger.gs
       - Pipeline: User Priorities          <- nanoninth account, pipeline_trigger.gs
  A check is breached when its newest dispatch run is older than 30 minutes.

Threshold evidence (30 days, 2026-09-26 to 2026-10-26, 20,567 dispatch runs):
dispatches arrive 24/7 including weekends, no quiet windows. Largest gap
between any two dispatch runs outside the outage = 5.6 min; largest gap per
heartbeat workflow = 10 min. The only gap over 8 min was the outage itself
(1,330 min from 2026-09-30 07:23 ET). GHA cron lateness cannot cause a false
alarm: the age is measured at run time against real run timestamps, so a late
watcher run only delays detection.

No email storm: a small JSON state file (carried between runs by actions/cache
in the workflow) records whether we are alerting. One email when a breach
starts or a new check joins it, a reminder every 6 hours while it lasts, one
recovery email when every check is healthy again. A missing state file means
"not alerting": the worst case of a lost cache is one extra email.

Behavior: healthy = one log line, no email, exit 0. GitHub API or email
failure = exit 1 (state not advanced, so the next run retries; GitHub's own
failed-workflow notification covers watcher self-death).

Usage:
    python dispatch_cadence_watcher.py --state-file PATH   # check + email
    python dispatch_cadence_watcher.py --dry-run           # print ages, never
        email, never write state
Token: GITHUB_TOKEN or GH_TOKEN env var; locally falls back to `gh auth token`.
"""

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dispatch_cadence_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
DEFAULT_REPO = "jamilmendez-ontel/local-pipeline"
THRESHOLD_MIN = 30
REMIND_HOURS = 6
ANY_DISPATCH = "any_dispatch"
# check name -> workflow file. None = every workflow in the repo.
CHECKS = {
    ANY_DISPATCH: None,
    "Pipeline: Daily Reports (Rolling)": "daily-reports-rolling.yml",
    "Pipeline: User Priorities": "pipeline-priorities.yml",
}
ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Pure logic (unit-tested; no network)
# ---------------------------------------------------------------------------

def parse_ts(value):
    """GitHub ISO timestamp ('2026-10-26T05:52:59Z') -> aware UTC datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate(newest, now, threshold_min=THRESHOLD_MIN):
    """newest: {check_name: datetime|None}. Returns (ages, breaches).

    ages: {check_name: minutes|None}. breaches: sorted check names whose newest
    dispatch run is older than the threshold, or that have none at all."""
    ages, breaches = {}, []
    for name, ts in newest.items():
        if ts is None:
            ages[name] = None
            breaches.append(name)
            continue
        age = (now - ts).total_seconds() / 60.0
        ages[name] = age
        if age > threshold_min:
            breaches.append(name)
    return ages, sorted(breaches)


def decide(state, breaches, now, remind_hours=REMIND_HOURS):
    """Return (action, new_state). action: none | alert | remind | recover.

    state: {"alerting": bool, "breached": [names], "last_email_at": iso|None}
    (empty dict when no state file exists yet)."""
    alerting = bool(state.get("alerting"))
    prior = set(state.get("breached") or [])
    last = parse_ts(state.get("last_email_at"))
    stamp = now.isoformat()

    if not breaches:
        if alerting:
            return "recover", {"alerting": False, "breached": [], "last_email_at": stamp}
        return "none", state

    if not alerting or set(breaches) - prior:
        action = "alert"
    elif last is None or now - last >= timedelta(hours=remind_hours):
        action = "remind"
    else:
        # Same outage, reminder not due. Keep last_email_at; track the set so
        # a check that recovers and breaks again counts as new.
        return "none", {"alerting": True, "breached": list(breaches),
                        "last_email_at": state.get("last_email_at")}
    return action, {"alerting": True, "breached": list(breaches), "last_email_at": stamp}


def _fmt_age(age):
    if age is None:
        return "no dispatch run found"
    if age >= 120:
        return f"{age / 60:.1f} hours ago"
    return f"{age:.0f} minutes ago"


def _label(name):
    return "Any dispatch-triggered workflow" if name == ANY_DISPATCH else name


def build_email(action, ages, newest, breaches, now, threshold_min=THRESHOLD_MIN):
    """Return (subject, body) for an alert, reminder or recovery email."""
    now_et = now.astimezone(ET)
    if action == "recover":
        subject = "Dispatch cadence: recovered, dispatches are arriving again"
        head = ["Dispatch cadence watcher: RECOVERED",
                "Every check is back under the threshold."]
    else:
        tag = "still stopped" if action == "remind" else "STOPPED"
        subject = f"Dispatch cadence: repository_dispatch runs {tag} ({len(breaches)} check(s))"
        head = [f"Dispatch cadence watcher: dispatches {tag}",
                f"No repository_dispatch run newer than {threshold_min:g} minutes "
                f"for: {', '.join(_label(b) for b in breaches)}."]
    lines = head + ["", f"Checked at: {now_et:%Y-%m-%d %I:%M %p} ET", ""]
    for name in CHECKS:
        if name not in ages:
            continue
        ts = newest.get(name)
        when = f" (at {ts.astimezone(ET):%Y-%m-%d %I:%M %p} ET)" if ts else ""
        mark = "BREACHED" if name in breaches else "ok"
        lines.append(f"  - [{mark}] {_label(name)}: newest run {_fmt_age(ages[name])}{when}")
    if action != "recover":
        lines += [
            "",
            "Most likely cause: the GitHub PAT in the Apps Script Script Properties "
            "(GITHUB_TOKEN) expired or was revoked. If only one heartbeat is "
            "breached, it is that Apps Script project: Daily Reports (Rolling) = "
            "ontel account daily_reports_trigger; User Priorities = nanoninth "
            "account pipeline_trigger. Also check the Apps Script trigger list "
            "and executions.",
            "Rotation map: every GITHUB_TOKEN Script Property (six ontel projects "
            "+ nanoninth pipeline_trigger) and the GHA secrets "
            "DATE_VALIDATOR_DISPATCH_PAT (local-pipeline) and COP_DISPATCH_PAT "
            "(gmail-scraper). Then re-fire missed nightlies with "
            "gh workflow run <wf>.yml -R jamilmendez-ontel/local-pipeline.",
            f"Reminder every {REMIND_HOURS} hours while this lasts; one email on recovery.",
        ]
    lines += ["", "(dispatch_cadence_watcher.py, Watch: Dispatch Cadence; silent when healthy)"]
    return subject, "\n".join(lines)


def load_state(path):
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        logger.warning(f"state file unreadable ({e}); treating as not alerting")
        return {}


def save_state(path, state):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# GitHub API (thin)
# ---------------------------------------------------------------------------

def get_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("No GITHUB_TOKEN/GH_TOKEN and `gh auth token` failed")


def newest_dispatch_run(repo, workflow_file, token):
    """created_at of the newest repository_dispatch run (None when none)."""
    base = f"https://api.github.com/repos/{repo}/actions"
    path = f"/workflows/{workflow_file}/runs" if workflow_file else "/runs"
    req = urllib.request.Request(
        f"{base}{path}?event=repository_dispatch&per_page=1",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        runs = json.load(resp).get("workflow_runs") or []
    return parse_ts(runs[0]["created_at"]) if runs else None


# ---------------------------------------------------------------------------
# Email (same Gmail API pattern and sender mask as pipeline_health_watcher)
# ---------------------------------------------------------------------------

def send_email(subject, body):
    from gmail_client import authenticate, masked_sender
    service = authenticate()
    msg = MIMEText(body, "plain")
    msg["To"] = ", ".join(RECIPIENTS)
    msg["From"] = masked_sender(service, "Pipeline Alerts")
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Email sent to {', '.join(RECIPIENTS)}: {subject}")


def _gha_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print ages and the would-be action; no email, no state write")
    parser.add_argument("--state-file", default=None,
                        help="JSON state path (carried between runs by actions/cache)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO))
    parser.add_argument("--threshold-min", type=float, default=THRESHOLD_MIN)
    args = parser.parse_args()

    token = get_token()
    now = datetime.now(timezone.utc)
    newest = {name: newest_dispatch_run(args.repo, wf, token) for name, wf in CHECKS.items()}
    ages, breaches = evaluate(newest, now, args.threshold_min)
    for name in CHECKS:
        logger.info(f"{_label(name)}: newest dispatch run {_fmt_age(ages[name])}")

    state = load_state(args.state_file)
    action, new_state = decide(state, breaches, now)
    logger.info(f"breaches={breaches or 'none'}; state alerting={bool(state.get('alerting'))}; "
                f"action={action}")

    if action != "none":
        subject, body = build_email(action, ages, newest, breaches, now, args.threshold_min)
        if args.dry_run:
            logger.info(f"Dry run: would send '{subject}'\n{body}")
        else:
            send_email(subject, body)  # raises on failure: state not advanced

    changed = new_state != state
    if args.state_file and changed and not args.dry_run:
        save_state(args.state_file, new_state)
    _gha_output("state_changed", "true" if changed and not args.dry_run else "false")
    if not breaches:
        logger.info("All dispatch checks green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
