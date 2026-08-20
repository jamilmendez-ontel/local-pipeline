#!/usr/bin/env python3
"""Schedule feed audit: Swift task-record schedules vs the task's activity feed.

Background (2026-08-14 investigation): Swift's server-side calendar scheduling
path applies its 12h noon->midnight normalization (meant for date-only
schedules) to timed schedules too, storing them 12 HOURS EARLY on the task
record while the activity feed (Firebase) keeps the correct instant. The feed
is therefore the source of truth whenever the two disagree.

This job compares every (or every recently-relevant) scheduled task in the
User Priorities report against its Firebase activity feed and maintains
pipeline.schedule_audit_anomalies:

  timed_mismatch   task and feed disagree on a real time (incl. the -12h flip
                   and reschedules that only reached one store)
  ghost_schedule   feed says the schedule was removed; task still scheduled
  no_feed_schedule task is scheduled but the feed has no schedule events

Benign and excluded: date-only schedules stored midnight-ET on the task vs
noon-ET in the feed (same calendar date, both correct by their own convention).

Serving: analytics.v_user_priorities_effective overlays feed corrections while
an anomaly is open AND the stored value is unchanged since detection.
data_staging.stg_user_priorities is never touched.

Modes:
  --mode incremental (default)  audit open anomalies + tasks scheduled within
                                [now-3d, now+45d]; a few hundred feed fetches
  --mode full                   audit every scheduled task (~5k fetches, ~4min)

Alerting (Gmail, "Pipeline Alerts" mask, jamil.mendez@ontel.co):
  - immediate email when a NEW timed_mismatch/ghost appears on a task whose
    schedule is current (due within the past day or in the future)
  - with --notify-schedulers, the scheduler (last_event_by) is ALSO emailed
    directly for both timed_mismatch and ghost_schedule (ghosts added
    2026-08-20 after the TENASKA - Horvath miss: members saw a false
    "Overdue" and nobody was told)
  - always email when the run FAILS (auth, coverage < FLOOR, db errors)
  - silent otherwise; run history in pipeline.schedule_audit_runs

Scheduling: GitHub Actions like the rest of this repo (Windows Task Scheduler
retired 2026-05-28; trigger via Apps Script repository_dispatch per house
convention). Logs print counts and task DIDs only - never member names - so
runs are safe in this public repo's action logs. Local manual runs need
Cloudflare WARP for the DB.
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, get_logger
from db import get_db

logger = get_logger("schedule_feed_audit")

ET = ZoneInfo("America/New_York")
FBURL = "https://swift-projects.firebaseio.com"
SCHEDULE_TYPES = {"schedule_add", "schedule_change", "schedule_override", "schedule_remove"}
TOLERANCE_MS = 2000
WORKERS = 8
COVERAGE_FLOOR = 0.90
ALERT_RECIPIENTS = ["jamil.mendez@ontel.co"]
# A feed event younger than this at detection time means Swift may still be
# propagating a legit change (remove/reschedule) to the task record - the
# report lags the feed by minutes. Skip flagging entirely and let the next
# run decide: a real anomaly persists, a propagating change clears itself.
# (First observed 2026-08-14: John Versoza's schedule removal was flagged as
# a ghost 2 minutes after the fact.)
FEED_EVENT_GRACE = timedelta(hours=1)

_token_lock = threading.Lock()
_tokens = {"fb": None, "id": None}


def _authenticate(force=False, seen=None):
    """seen: the token the caller's failed request used. If another thread
    already refreshed it, return without a redundant login - Swift's auth
    endpoint throttles repeated logins, and 8 workers hitting a 401 at the
    same expiry must not each re-login (up to ~10min backoff EACH, serially,
    inside the lock)."""
    with _token_lock:
        if _tokens["fb"] and not force:
            return
        if force and seen is not None and _tokens["fb"] != seen:
            return  # a sibling thread already re-authenticated
        for attempt in range(2 if force else 6):
            r = requests.post(
                f"{SWIFT_BASE_URL}/api/auth/token",
                headers={"Content-Type": "application/json"},
                json={"grantType": "password", "include": ["profile", "firebaseToken"],
                      "username": SWIFT_USERNAME, "password": SWIFT_PASSWORD, "scope": "openid"},
                timeout=60,
            )
            payload = r.json()
            if "firebaseToken" in payload:
                _tokens["fb"] = payload["firebaseToken"]
                _tokens["id"] = payload["idToken"]
                return
            wait = 30 * (attempt + 1)
            logger.warning(f"auth attempt {attempt + 1} rejected "
                           f"({payload.get('message', r.status_code)}); waiting {wait}s")
            time.sleep(wait)
        raise RuntimeError("Swift authentication failed after retries")


session = requests.Session()
session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=WORKERS, pool_maxsize=WORKERS * 2))


def fetch_report_rows():
    """Current User Priorities report (tz=UTC), keyed by Task DID."""
    all_statuses = ["pending", "in_progress", "has_rejection",
                    "submitted", "approved", "rejected", "cancelled"]
    target_statuses = ["pending", "in_progress", "has_rejection"]
    rows = {}
    for target in target_statuses:
        fo = quote(json.dumps({"status": {s: False for s in all_statuses if s != target}}))
        page = 0
        while True:
            r = session.get(
                f"{SWIFT_BASE_URL}/api/next/user-priorities/_report"
                f"?pageSize=1000&page={page}&filterOptions={fo}"
                f"&tz=UTC&dateFormat=yyyy-MM-dd%27T%27HH%3Amm%3AssZ",
                headers={"Authorization": f"Bearer {_tokens['id']}", "Accept": "application/json"},
                timeout=60,
            )
            if r.status_code == 204:
                break
            r.raise_for_status()
            body = r.json()
            # A 200 without 'list' (throttle message, maintenance page, dead
            # token) must NOT read as end-of-data: a silently truncated report
            # would mass-resolve real anomalies as "vanished".
            if "list" not in body:
                raise RuntimeError(
                    f"report page returned 200 without 'list' "
                    f"(status={target}, page={page}): {str(body)[:200]}")
            data = body["list"]
            if not data:
                break
            for rec in data:
                did = rec.get("Task DID")
                if did:
                    rows[did] = rec
            page += 1
    return rows


def fetch_feed(task_did):
    for attempt in (1, 2):
        tok = _tokens["fb"]
        r = session.get(
            f"{FBURL}/asset-tasks-meta/{task_did}/links/feed.json",
            params={"auth": tok}, timeout=60,
        )
        if r.status_code == 401 and attempt == 1:
            _authenticate(force=True, seen=tok)
            continue
        if r.status_code in (429, 500, 502, 503) and attempt == 1:
            time.sleep(2)
            continue
        r.raise_for_status()
        return r.json()
    return None


def parse_scheduled(rec):
    s = (rec.get("Scheduled") or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning(f"unparseable Scheduled {s!r} on {rec.get('Task DID')}")
        return None


def is_benign_date_only(stored, feed):
    """Task=midnight ET, feed=noon ET, same ET calendar date: both encodings
    of the same date-only schedule."""
    s_et, f_et = stored.astimezone(ET), feed.astimezone(ET)
    return ((s_et.hour, s_et.minute) == (0, 0)
            and (f_et.hour, f_et.minute) == (12, 0)
            and s_et.date() == f_et.date())


def classify(task_did, stored):
    """Return (verdict, detail) for one task. verdict in
    {'ok', 'timed_mismatch', 'ghost_schedule', 'no_feed_schedule', 'fetch_error'}."""
    detail = {"feed_scheduled": None, "offset_hours": None,
              "last_feed_event": None, "last_event_at": None, "last_event_by": None}
    try:
        feed = fetch_feed(task_did)
    except Exception as e:
        detail["last_feed_event"] = f"fetch error: {e}"[:200]
        return "fetch_error", detail

    # Parse failures on ONE malformed feed event must degrade to fetch_error
    # for that task only - an unguarded raise here would propagate through
    # ex.map and abort the whole run, permanently (bad Firebase data does not
    # self-heal, so every subsequent run would die on the same event).
    try:
        events = [e for e in (feed or {}).values()
                  if isinstance(e, dict) and e.get("type") in SCHEDULE_TYPES]
        if not events:
            return "no_feed_schedule", detail

        events.sort(key=lambda e: e.get("date", 0))
        last = events[-1]
        data = last.get("data") or {}
        detail["last_feed_event"] = last.get("type")
        detail["last_event_at"] = datetime.fromtimestamp(
            last.get("date", 0) / 1000, tz=timezone.utc)
        detail["last_event_by"] = (data.get("changedBy") or {}).get("value")

        if last["type"] == "schedule_remove":
            p = data.get("p_schedule")
            if isinstance(p, dict) and "date" in p:
                detail["feed_scheduled"] = datetime.fromtimestamp(
                    p["date"] / 1000, tz=timezone.utc)
            return "ghost_schedule", detail

        c = data.get("c_schedule")
        if not (isinstance(c, dict) and "date" in c):
            return "no_feed_schedule", detail

        feed_dt = datetime.fromtimestamp(c["date"] / 1000, tz=timezone.utc)
        detail["feed_scheduled"] = feed_dt
        diff_ms = (stored - feed_dt).total_seconds() * 1000
        if abs(diff_ms) <= TOLERANCE_MS or is_benign_date_only(stored, feed_dt):
            return "ok", detail
        detail["offset_hours"] = round(diff_ms / 3600000, 2)
        return "timed_mismatch", detail
    except Exception as e:
        logger.warning(f"feed parse error on {task_did}: {e}")
        detail["last_feed_event"] = f"parse error: {e}"[:200]
        return "fetch_error", detail


def send_alert(subject, html_body, recipients=None, sender_name="Pipeline Alerts"):
    """sender_name: "Pipeline Alerts" for ops mail to Jamil;
    "Ontel Schedule Check" for member-facing scheduler notices."""
    try:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        import base64
        import gmail_client
        from gmail_client import authenticate, masked_sender

        # Headless guard: with no stored token, authenticate() would launch
        # the interactive browser OAuth flow (run_local_server) and hang the
        # job forever under a scheduler. Fail fast instead.
        if not gmail_client.TOKEN_FILE.exists():
            raise RuntimeError(f"gmail token missing ({gmail_client.TOKEN_FILE}); "
                               f"refusing interactive OAuth in a headless job")
        service = authenticate()
        msg = MIMEMultipart()
        msg["To"] = ", ".join(recipients or ALERT_RECIPIENTS)
        msg["From"] = masked_sender(service, sender_name)
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))
        service.users().messages().send(
            userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        ).execute()
        logger.info(f"alert sent to {msg['To']}: {subject}")
    except Exception as e:
        logger.error(f"alert email failed: {e}")


def resolve_scheduler_email(db, swift_name):
    """Map a Swift display name ('Abbie Cariño') to a directory email.
    Directory holds full legal names ('Abbie Clare Deoferio Cariño'), so match
    on first token + last token; only a UNIQUE match counts."""
    if not swift_name or " " not in swift_name.strip():
        return None
    tokens = swift_name.strip().split()
    rows = db.fetch(
        "SELECT email FROM analytics.v_employee_directory "
        "WHERE full_name ILIKE $1 AND full_name ILIKE $2 AND email IS NOT NULL",
        tokens[0] + "%", "%" + tokens[-1])
    return rows[0]["email"] if len(rows) == 1 else None


def scheduler_notice_html(verdict, rec, stored, d):
    """Friendly per-scheduler notice (Abbie-message style): blameless, names
    both times, tells them the one safe fix."""
    if verdict == "ghost_schedule":
        if d["feed_scheduled"]:
            situation = (
                f"you changed its schedule (Swift's activity feed correctly shows "
                f"<b>{_fmt_et(d['feed_scheduled'])}</b>), but Swift kept the old copy "
                f"<b>{_fmt_et(stored)}</b> on the task record. Alarms and reports "
                f"read that old copy, so the task can wrongly show \"past due\".")
        else:
            situation = (
                f"you removed its schedule, but Swift kept a leftover copy "
                f"<b>{_fmt_et(stored)}</b> on the task record. Alarms and reports "
                f"read that leftover copy, so the task can wrongly show \"past due\". "
                f"If you meant to remove the schedule entirely, use the Reschedule "
                f"dialog once to set and clear it so the leftover copy goes away.")
    else:  # timed_mismatch
        situation = (
            f"you scheduled it for <b>{_fmt_et(d['feed_scheduled'])}</b> (Swift's activity "
            f"feed shows this correctly), but Swift saved a hidden second copy as "
            f"<b>{_fmt_et(stored)}</b>. Alarms and reports read the hidden copy, so the "
            f"task will show \"due soon\"/\"past due\" about 12 hours early.")
    return (
        f"<p>Hi {(d['last_event_by'] or 'there').split()[0]},</p>"
        f"<p>Heads up on a schedule you set in Swift. This is a known Swift bug, "
        f"not a mistake on your side, and your computer settings are fine.</p>"
        f"<p><b>{rec.get('Task Name')}</b> on <b>{rec.get('Asset Name')}</b> "
        f"({rec.get('Project')}):<br>{situation}</p>"
        f"<p><b>The fix takes a minute:</b> open the task and use the "
        f"<b>Reschedule</b> dialog to set the time again. That path always saves "
        f"correctly. Scheduling from the calendar view is what triggers the bug.</p>"
        f"<p>(Automated notice from the Ontel data team's schedule check. "
        f"Questions: reply to this email.)</p>")


def _fmt_et(dt):
    return dt.astimezone(ET).strftime("%Y-%m-%d %I:%M %p ET") if dt else "(none)"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    ap.add_argument("--no-email", action="store_true", help="suppress alert emails (dry runs)")
    ap.add_argument("--notify-schedulers", action="store_true",
                    help="ALSO email the scheduler directly on a new anomaly "
                         "(default off: their address is shown in Jamil's alert "
                         "instead; enable only after Jamil approves a sample)")
    ap.add_argument("--past-days", type=int, default=3)
    ap.add_argument("--future-days", type=int, default=45)
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    db = None
    run_id = None

    def finish(status, error=None, **counts):
        if run_id is None:
            return
        db.execute(
            "UPDATE pipeline.schedule_audit_runs SET finished_at = now(), status = $2, "
            "error = $3, tasks_checked = $4, feeds_fetched = $5, coverage_pct = $6, "
            "new_anomalies = $7, resolved_anomalies = $8, open_anomalies = $9 "
            "WHERE run_id = $1",
            run_id, status, error,
            counts.get("tasks_checked"), counts.get("feeds_fetched"),
            counts.get("coverage_pct"), counts.get("new"), counts.get("resolved"),
            counts.get("open"))

    try:
        # DB init INSIDE the try so an unreachable DB (WARP off - the most
        # common failure on this host) still produces the failure email.
        db = get_db()

        # Self-heal runs orphaned by a kill/sleep (their 'running' row never
        # gets finished), then bail if another audit genuinely looks live.
        db.execute(
            "UPDATE pipeline.schedule_audit_runs SET status = 'failed', "
            "finished_at = now(), error = 'stale running row - process died' "
            "WHERE status = 'running' AND started_at < now() - interval '3 hours'")
        live = db.fetchval(
            "SELECT count(*) FROM pipeline.schedule_audit_runs "
            "WHERE status = 'running'")
        if live:
            logger.info("another audit run appears live; exiting (no overlap)")
            return 0

        run_id = db.fetchval(
            "INSERT INTO pipeline.schedule_audit_runs (mode) VALUES ($1) RETURNING run_id",
            args.mode)

        _authenticate()
        report = fetch_report_rows()
        scheduled = {did: rec for did, rec in report.items() if parse_scheduled(rec)}

        # Report sanity floor: stg_user_priorities (refreshed every 5 min from
        # the same report) is the expected size. A big shortfall means the
        # report pull was truncated - mutating the registry then would
        # mass-resolve real anomalies as "vanished". Refuse instead.
        expected = db.fetchval(
            "SELECT count(*) FROM data_staging.stg_user_priorities "
            "WHERE scheduled IS NOT NULL AND task_did IS NOT NULL")
        if expected and len(scheduled) < 0.7 * expected:
            raise RuntimeError(
                f"report sanity floor: pulled {len(scheduled)} scheduled tasks "
                f"vs {expected} in stg (<70%) - refusing to touch the registry")
        open_rows = db.fetch(
            "SELECT task_did, stored_scheduled FROM pipeline.schedule_audit_anomalies "
            "WHERE status = 'open'")
        # task_did -> stored value we flagged; used both for membership and to
        # detect "rescheduled but broken AGAIN" (stored changed, still mismatched)
        open_dids = {r["task_did"]: r["stored_scheduled"] for r in open_rows}

        if args.mode == "full":
            candidates = dict(scheduled)
        else:
            lo = started - timedelta(days=args.past_days)
            hi = started + timedelta(days=args.future_days)
            candidates = {did: rec for did, rec in scheduled.items()
                          if lo <= parse_scheduled(rec) <= hi}
            for did in open_dids:
                if did in scheduled:
                    candidates.setdefault(did, scheduled[did])
        logger.info(f"mode={args.mode}: {len(candidates)} of {len(scheduled)} "
                    f"scheduled tasks to audit ({len(open_dids)} anomalies open)")

        # Open anomalies whose task vanished from the report (completed /
        # unscheduled / status moved on): nothing left to correct.
        vanished = set(open_dids) - set(scheduled)
        for did in vanished:
            db.execute(
                "UPDATE pipeline.schedule_audit_anomalies SET status = 'resolved', "
                "resolved_at = now() WHERE task_did = $1 AND status = 'open'", did)

        results = {}
        lock = threading.Lock()

        def work(item):
            did, rec = item
            res = classify(did, parse_scheduled(rec))
            with lock:
                results[did] = res

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(work, candidates.items()))

        fetched = sum(1 for v, _ in results.values() if v != "fetch_error")
        coverage = fetched / len(candidates) if candidates else 1.0

        new_alerts = []
        n_new = n_resolved = 0
        for did, (verdict, d) in results.items():
            rec = candidates[did]
            stored = parse_scheduled(rec)
            if verdict in ("ok", "fetch_error"):
                if verdict == "ok" and did in open_dids:
                    db.execute(
                        "UPDATE pipeline.schedule_audit_anomalies SET status = 'resolved', "
                        "resolved_at = now() WHERE task_did = $1 AND status = 'open'", did)
                    n_resolved += 1
                continue

            # Grace: a NOT-yet-open disagreement whose latest feed event is
            # very fresh is most likely a legit change still propagating to
            # the task record. Skip it; the next run flags it if it stuck.
            if (did not in open_dids and d["last_event_at"] is not None
                    and started - d["last_event_at"] < FEED_EVENT_GRACE):
                logger.info(f"grace-skip {did}: feed event "
                            f"{started - d['last_event_at']} old ({verdict})")
                continue

            was_open = did in open_dids
            db.execute(
                "INSERT INTO pipeline.schedule_audit_anomalies "
                "(task_did, class, status, stored_scheduled, feed_scheduled, offset_hours, "
                " last_feed_event, last_event_at, last_event_by, task_name, asset_name, project) "
                "VALUES ($1, $2, 'open', $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                "ON CONFLICT (task_did) DO UPDATE SET "
                "class = EXCLUDED.class, status = 'open', resolved_at = NULL, "
                "stored_scheduled = EXCLUDED.stored_scheduled, "
                "feed_scheduled = EXCLUDED.feed_scheduled, "
                "offset_hours = EXCLUDED.offset_hours, "
                "last_feed_event = EXCLUDED.last_feed_event, "
                "last_event_at = EXCLUDED.last_event_at, "
                "last_event_by = EXCLUDED.last_event_by, "
                "last_seen_at = now()",
                did, verdict, stored, d["feed_scheduled"], d["offset_hours"],
                d["last_feed_event"], d["last_event_at"], d["last_event_by"],
                rec.get("Task Name"), rec.get("Asset Name"), rec.get("Project"))
            # Alert on (a) a brand-new anomaly, or (b) an open anomaly whose
            # stored value CHANGED and is still wrong - i.e. the scheduler
            # rescheduled and Swift flipped it again. An unchanged open
            # anomaly stays silent: one email per entry per breakage.
            rebroken = was_open and open_dids[did] != stored
            if not was_open:
                n_new += 1
            if not was_open or rebroken:
                # Only for current schedules where the wrong value can still
                # mislead someone (due recently or in the future).
                relevant = max(filter(None, [stored, d["feed_scheduled"]]))
                if (verdict in ("timed_mismatch", "ghost_schedule")
                        and relevant >= started - timedelta(days=1)):
                    new_alerts.append((verdict, rec, stored, d))

        n_resolved += len(vanished)
        n_open = db.fetchval(
            "SELECT count(*) FROM pipeline.schedule_audit_anomalies WHERE status = 'open'")

        status = "ok" if coverage >= COVERAGE_FLOOR else "failed"
        err = None if status == "ok" else \
            f"coverage {coverage:.0%} below floor {COVERAGE_FLOOR:.0%} - feeds unreachable?"
        finish(status, err, tasks_checked=len(candidates), feeds_fetched=fetched,
               coverage_pct=round(coverage * 100, 1), new=n_new,
               resolved=n_resolved, open=n_open)
        logger.info(f"done: {len(candidates)} checked, coverage {coverage:.0%}, "
                    f"{n_new} new, {n_resolved} resolved, {n_open} open")

        if not args.no_email:
            if new_alerts:
                items = []
                for v, r, s, d in new_alerts:
                    sched_email = resolve_scheduler_email(db, d["last_event_by"])
                    notified = ""
                    if sched_email and v in ("timed_mismatch", "ghost_schedule"):
                        if args.notify_schedulers:
                            send_alert(
                                f"Schedule needs a quick re-do: {r.get('Task Name')} "
                                f"- {r.get('Asset Name')}",
                                scheduler_notice_html(v, r, s, d),
                                recipients=[sched_email],
                                sender_name="Ontel Schedule Check")
                            notified = f" - scheduler notified: {sched_email}"
                        else:
                            notified = (f" - would notify scheduler: {sched_email} "
                                        f"(enable --notify-schedulers)")
                    elif d["last_event_by"] and not sched_email:
                        notified = (f" - scheduler '{d['last_event_by']}' not uniquely "
                                    f"matched in directory; manual follow-up")
                    items.append(
                        f"<li><b>{r.get('Task Name')}</b> / {r.get('Asset Name')} "
                        f"({r.get('Project')})<br>"
                        f"class: {v} - task record says <b>{_fmt_et(s)}</b>, "
                        f"activity feed says <b>{_fmt_et(d['feed_scheduled'])}</b> "
                        f"(last feed event: {d['last_feed_event']} by {d['last_event_by']})"
                        f"{notified}<br>"
                        f"Fix: reschedule via the <b>Reschedule dialog</b> (saves correctly); "
                        f"the warehouse view already serves the feed value.</li>")
                send_alert(
                    f"Swift schedule audit: {len(new_alerts)} new schedule anomal"
                    f"{'y' if len(new_alerts) == 1 else 'ies'}",
                    f"<p>New Swift task-vs-feed schedule disagreements "
                    f"(the 12h calendar-path bug or a one-store reschedule):</p>"
                    f"<ul>{''.join(items)}</ul>"
                    f"<p>Registry: pipeline.schedule_audit_anomalies | corrected data: "
                    f"analytics.v_user_priorities_effective</p>")
            if status == "failed":
                send_alert("Swift schedule audit FAILED",
                           f"<p>{err}</p><p>run_id: {run_id}, mode: {args.mode}, "
                           f"checked {len(candidates)}, fetched {fetched}.</p>")
        return 0 if status == "ok" else 1

    except Exception as e:
        logger.exception("audit run failed")
        try:
            finish("failed", str(e)[:500])
        except Exception:
            pass
        if not args.no_email:
            send_alert("Swift schedule audit FAILED",
                       f"<p>Unhandled error: {e}</p><p>mode: {args.mode}</p>")
        return 1


if __name__ == "__main__":
    sys.exit(main())
