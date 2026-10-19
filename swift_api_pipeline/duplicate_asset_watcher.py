#!/usr/bin/env python3
"""Duplicate asset watcher: find Swift assets created twice (same project, same
path, same name, different asset ids) and email which copy to keep.

Why: v1 has no asset id on a timer or a QA form. data_staging.backfill_asset_did()
matches on site name + path and leaves asset_did NULL when two assets share
both, so every timer on such a site loses its asset. Measured 2026-10-19: 58
duplicate groups (126 assets, 112 untouched) in TS13-TS20; 98 v1 timers NULL
because of them; TS20's WINCHESTER pair failed the timer v2 parity audit two
nights running. Deleting the empty copy in Swift fixes v1 on its next load.

Per group, an asset is ACTIVE when any task left 'pending' or a timer (v2,
data_staging.stg_timer_activities_inc, which carries Swift's own asset id)
points at it. Verdicts:
  delete_empty  one active copy: KEEP it, DELETE the empty ones.
  all_empty     no active copy: KEEP the oldest (by the Swift id's creation
                time), DELETE the rest.
  merge_needed  two or more active copies: nothing is safe to delete; someone
                moves the work onto one copy first.

Cadence (stateless, no run table): every run emails the groups whose newest
copy was created in the last NEW_HOURS, so ops can delete it before anyone
starts a timer on it; on Mondays (ET) it emails the full list. Nothing to
report = one log line, no email, exit 0. Crash = exit 1 (GitHub's workflow
failure notification covers watcher self-death).

The watcher never writes anything. Recipients: Jamil only until he names the
ops recipients (send rule: automated sends go to jamil.mendez@ontel.co first).

Usage:
    python duplicate_asset_watcher.py              # check + email
    python duplicate_asset_watcher.py --dry-run    # check + print; no email
    python duplicate_asset_watcher.py --full       # full list regardless of the day
"""

import argparse
import base64
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import get_logger, get_db, close_db, retry_db, setup_logging

setup_logging()
logger = get_logger("duplicate_asset_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
NEW_HOURS = 26          # daily cron + up to 2 h GHA lag, with no gap between runs
ET = ZoneInfo("America/New_York")
PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"

DUPLICATES_SQL = """
WITH g AS (
    SELECT project_did, asset_id, trim(asset_name) AS aname
    FROM data_staging.stg_assets
    WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING count(DISTINCT asset_did) > 1
)
SELECT p.project_name, s.project_did, s.asset_id, g.aname AS asset_name, s.asset_did,
       s.task_count, s.task_count - s.tasks_pending AS touched,
       (SELECT count(*) FROM data_staging.stg_timer_activities_inc t
         WHERE t.asset_did = s.asset_did AND t.vanished_at IS NULL) AS timers
FROM data_staging.stg_assets s
JOIN g ON g.project_did = s.project_did
      AND g.asset_id IS NOT DISTINCT FROM s.asset_id
      AND g.aname = trim(s.asset_name)
LEFT JOIN data_staging.stg_projects p ON p.project_did = s.project_did
ORDER BY p.project_name, g.aname, s.asset_did
"""


def push_created_at(did):
    """Creation time encoded in the first 8 chars of a Firebase push id."""
    if not did or len(did) < 8 or any(c not in PUSH_CHARS for c in did[:8]):
        return None
    ms = 0
    for c in did[:8]:
        ms = ms * 64 + PUSH_CHARS.index(c)
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _created_key(a):
    return push_created_at(a["asset_did"]) or datetime.max.replace(tzinfo=timezone.utc)


def group_rows(rows):
    groups = {}
    for r in rows:
        key = (r["project_did"], r["asset_id"], r["asset_name"])
        groups.setdefault(key, {"project_name": r["project_name"], "asset_id": r["asset_id"],
                                "asset_name": r["asset_name"], "assets": []})["assets"].append(dict(r))
    return list(groups.values())


def classify_group(assets):
    """assets: one group's rows. Returns the group dict with verdict/keep/remove."""
    assets = sorted(assets, key=_created_key)
    for a in assets:
        a["active"] = (a["touched"] or 0) > 0 or (a["timers"] or 0) > 0
        a["created_at"] = push_created_at(a["asset_did"])
    active = [a for a in assets if a["active"]]
    if len(active) > 1:
        verdict, keep = "merge_needed", None
    elif len(active) == 1:
        verdict, keep = "delete_empty", active[0]["asset_did"]
    else:
        verdict, keep = "all_empty", assets[0]["asset_did"]
    remove = [] if keep is None else [a["asset_did"] for a in assets if a["asset_did"] != keep]
    first = assets[0]
    return {"project_name": first["project_name"], "asset_id": first["asset_id"],
            "asset_name": first["asset_name"], "assets": assets,
            "verdict": verdict, "keep": keep, "remove": remove}


def select_groups(groups, now, full):
    if full:
        return list(groups)
    cutoff = now - timedelta(hours=NEW_HOURS)
    return [g for g in groups
            if any(a["created_at"] and a["created_at"] >= cutoff for a in g["assets"])]


def is_full_list_day(now):
    return now.astimezone(ET).weekday() == 0


VERDICT_TEXT = {
    "delete_empty": "one copy has work on it; delete the empty copy",
    "all_empty": "no copy has work yet; keep the oldest, delete the rest",
    "merge_needed": "MERGE: more than one copy has work on it; move the work onto one copy before deleting",
}


def build_email_body(groups, now, full, total):
    et = now.astimezone(ET)
    head = ("Full weekly list" if full else f"New in the last {NEW_HOURS} h")
    lines = [
        f"Duplicate Swift assets: {head}, {len(groups)} group(s) "
        f"({total} in Swift today). Checked {et:%a %b %d, %Y %I:%M %p} ET.",
        "",
        "Each group is one site created more than once in the same project, with the same",
        "path and name. Until the extra copy is deleted, the timer and QA form reports cannot",
        "tell which copy a timer belongs to and leave its asset empty.",
        "",
    ]
    for g in groups:
        lines += [f"{g['project_name']} | {g['asset_name']}",
                  f"  Path: {g['asset_id']}",
                  f"  {VERDICT_TEXT[g['verdict']]}"]
        for a in g["assets"]:
            if g["verdict"] == "merge_needed":
                tag = "HAS WORK"
            elif a["asset_did"] == g["keep"]:
                tag = "KEEP"
            else:
                tag = "DELETE"
            created = (a["created_at"].astimezone(ET).strftime("%b %d %Y %I:%M:%S %p ET")
                       if a["created_at"] else "unknown")
            lines.append(f"    {tag:<8} {a['asset_did']}  created {created}, "
                         f"{a['touched']} tasks touched of {a['task_count']}, {a['timers']} timers")
        lines.append("")
    lines.append("Sent by duplicate_asset_watcher.py (local-pipeline). It never changes Swift or the database.")
    return "\n".join(lines)


def send_email(body, subject):
    import gmail_client
    from gmail_client import authenticate, masked_sender
    if not gmail_client.TOKEN_FILE.exists():
        raise RuntimeError(f"gmail token missing ({gmail_client.TOKEN_FILE}); "
                           f"refusing interactive OAuth in a headless job")
    service = authenticate()
    msg = MIMEText(body, "plain")
    msg["To"] = ", ".join(RECIPIENTS)
    msg["From"] = masked_sender(service, "Pipeline Alerts")
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=3)
    logger.info(f"Email sent to {', '.join(RECIPIENTS)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print; no email")
    parser.add_argument("--full", action="store_true", help="full list regardless of the day")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    full = args.full or is_full_list_day(now)
    db = None
    try:
        db = get_db()
        rows = retry_db(lambda: db.fetch(DUPLICATES_SQL), description="read duplicate assets")
        groups = [classify_group(g["assets"]) for g in group_rows(rows)]
        picked = select_groups(groups, now, full)
        counts = {v: sum(1 for g in groups if g["verdict"] == v) for v in VERDICT_TEXT}
        logger.info(f"{len(groups)} duplicate group(s) in Swift {counts}; "
                    f"{len(picked)} to report ({'full list' if full else f'new in {NEW_HOURS} h'})")
        if not picked:
            logger.info("Nothing to report; no email.")
            return 0
        body = build_email_body(picked, now, full, len(groups))
        merges = sum(1 for g in picked if g["verdict"] == "merge_needed")
        subject = (f"Duplicate Swift assets: {len(picked)} "
                   f"{'group(s), weekly list' if full else 'new group(s)'}"
                   + (f", {merges} need a merge" if merges else ""))
        if args.dry_run:
            logger.info(f"Dry run: email suppressed.\nSubject: {subject}\n{body}")
        else:
            send_email(body, subject)
        return 0
    except Exception:
        logger.exception("duplicate asset watcher crashed")
        return 1
    finally:
        if db is not None:
            close_db()


if __name__ == "__main__":
    sys.exit(main())
