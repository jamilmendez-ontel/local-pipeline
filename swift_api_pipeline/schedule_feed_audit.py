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
                   and reschedules that only reached one store; ALSO includes
                   half-applied removes where the record keeps exactly the
                   12h flip of the removed TIMED value - TENASKA 2026-08-18
                   showed the schedule stays live in one store there)
  ghost_schedule   feed says the schedule was removed; task still scheduled
                   (record keeps the removed value itself, not its flip)
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
  - with --notify-schedulers, a member-facing notice ALSO goes out (added
    2026-08-20 after the TENASKA - Horvath miss: members saw a false
    "Overdue" and nobody was told). Audience by class:
      timed_mismatch -> notice team (all active Data Analysts + Project
        Associates, LIVE from analytics.v_employee_directory) + the
        directory-matched scheduler (last_event_by)
      ghost_schedule -> Jamil ONLY for now (2026-08-20): he confirms these
        with the team before the audience widens
  - when a noticed anomaly resolves, an all-clear follow-up is sent as a
    REPLY on the original notice's Gmail thread (thread ids stored on the
    anomaly row, migration 238), so members know the fix landed
  - fresh feed events (< FRESH_EVENT_WINDOW) are NOT punted to the next run:
    they get an in-run recheck (RECHECK_DELAY wait + report re-pull) and
    alert in the same run if still inconsistent (alarm ASAP, 2026-08-20)
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
# Member-facing scheduler notices go to all active DAs + PAs, LIVE from the
# directory (Jamil 2026-08-20; was a hardcoded 4-person list for ~1 hour),
# plus the directory-matched scheduler appended by the caller.
NOTICE_TEAM_POSITIONS = ("Data Analyst%", "Project Associate%")
# A feed event younger than this at detection time means Swift may still be
# propagating a legit change (remove/reschedule) to the task record - the
# report lags the feed by minutes. (First observed 2026-08-14: John Versoza's
# schedule removal was flagged as a ghost 2 minutes after the fact.)
# Until 2026-08-20 these were skipped until the NEXT run (worst case ~2h to
# alarm). Now they get an in-run recheck instead: wait RECHECK_DELAY, re-pull
# the report, re-classify; still inconsistent -> alert in THIS run.
FRESH_EVENT_WINDOW = timedelta(hours=1)
RECHECK_DELAY = timedelta(seconds=120)

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
                feed_dt = datetime.fromtimestamp(p["date"] / 1000, tz=timezone.utc)
                detail["feed_scheduled"] = feed_dt
                # Half-applied removes (TENASKA, 2026-08-18): Swift can drop a
                # schedule from one store only - Mary's remove left the task
                # record holding the 12h-FLIPPED copy of her add, and the
                # schedule store still had the original (Esther
                # schedule_change'd it FROM that value the next morning). When
                # the record holds exactly removed-value-minus-12h on a TIMED
                # schedule, the schedule effectively still exists and members
                # see the flip symptom (false overdue), so classify as
                # timed_mismatch: full notice audience + the view serves the
                # feed value instead of NULL. Date-only removes are excluded
                # (record midnight == feed noon - 12h by CONVENTION, not flip).
                f_et = feed_dt.astimezone(ET)
                diff_ms = (stored - feed_dt).total_seconds() * 1000
                if ((f_et.hour, f_et.minute) != (12, 0)
                        and abs(diff_ms + 12 * 3600 * 1000) <= TOLERANCE_MS):
                    detail["offset_hours"] = round(diff_ms / 3600000, 2)
                    return "timed_mismatch", detail
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


def send_alert(subject, html_body, recipients=None, sender_name="Pipeline Alerts",
               thread_id=None, in_reply_to=None):
    """sender_name: "Pipeline Alerts" for ops mail to Jamil;
    "Ontel Schedule Check" for member-facing scheduler notices.

    thread_id + in_reply_to (the original's Gmail threadId and RFC 822
    Message-ID) make this a REPLY on that thread - used by the all-clear
    follow-up. Returns {"thread_id", "message_id"} of the sent mail (so the
    caller can store them for later replies), or None on failure."""
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
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.attach(MIMEText(html_body, "html"))
        body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            body["threadId"] = thread_id
        sent = service.users().messages().send(userId="me", body=body).execute()
        logger.info(f"alert sent to {msg['To']}: {subject}")
        # The RFC 822 Message-ID is assigned at send time; fetch it back so a
        # later reply can reference it.
        message_id = None
        try:
            meta = service.users().messages().get(
                userId="me", id=sent["id"], format="metadata",
                metadataHeaders=["Message-ID"]).execute()
            message_id = next(
                (h["value"] for h in meta.get("payload", {}).get("headers", [])
                 if h.get("name", "").lower() == "message-id"), None)
        except Exception as e:
            logger.warning(f"could not fetch Message-ID of sent alert: {e}")
        return {"thread_id": sent.get("threadId"), "message_id": message_id}
    except Exception as e:
        logger.error(f"alert email failed: {e}")
        return None


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
        # Post flip-signature reclass (2026-08-20), ghosts are TRUE removals:
        # feed_scheduled is the REMOVED value (usually == stored), never a
        # newer schedule - so always the "removed but leftover" wording.
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


def notice_team(db):
    """Member-facing notice audience, LIVE from the directory: active Data
    Analysts + Project Associates (Jamil 2026-08-20). Falls back to ops so a
    notice is never silently dropped if the directory query comes back empty."""
    try:
        rows = db.fetch(
            "SELECT email FROM analytics.v_employee_directory "
            "WHERE is_active AND email IS NOT NULL "
            "AND (position LIKE $1 OR position LIKE $2) ORDER BY email",
            *NOTICE_TEAM_POSITIONS)
        team = [r["email"] for r in rows]
    except Exception as e:
        logger.error(f"notice team query failed: {e}")
        team = []
    return team or list(ALERT_RECIPIENTS)


def all_clear_html(row):
    """Follow-up on the original notice thread once the anomaly resolves.
    Neutral wording: 'resolved' can mean rescheduled, completed, or
    unscheduled - not necessarily a manual fix."""
    return (
        f"<p>All clear on this one: <b>{row['task_name']}</b> on "
        f"<b>{row['asset_name']}</b> no longer has a schedule inconsistency - "
        f"the task record and Swift's activity feed agree again (rescheduled, "
        f"completed, or cleared). Nothing more to do on your side.</p>"
        f"<p>(Automated notice from the Ontel data team's schedule check.)</p>")


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

        resolve_sql = (
            "UPDATE pipeline.schedule_audit_anomalies SET status = 'resolved', "
            "resolved_at = now() WHERE task_did = $1 AND status = 'open' "
            "RETURNING task_did, task_name, asset_name, notice_thread_id, "
            "notice_message_id, notice_subject, notice_recipients, "
            "resolved_notice_sent_at")
        all_clear = []

        def resolve_anomaly(did):
            """Mark resolved; if the original member notice went out and has
            no follow-up yet, queue the all-clear reply on its thread."""
            row = db.fetchrow(resolve_sql, did)
            if (row and row["notice_thread_id"]
                    and row["resolved_notice_sent_at"] is None):
                all_clear.append(row)

        # Open anomalies whose task vanished from the report (completed /
        # unscheduled / status moved on): nothing left to correct.
        vanished = set(open_dids) - set(scheduled)
        for did in vanished:
            resolve_anomaly(did)

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

        def record_anomaly(did, verdict, rec, stored, d):
            """Upsert one confirmed anomaly; queue alerts per the
            once-per-entry-per-breakage rule."""
            nonlocal n_new
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

        recheck = {}
        for did, (verdict, d) in results.items():
            rec = candidates[did]
            stored = parse_scheduled(rec)
            if verdict in ("ok", "fetch_error"):
                if verdict == "ok" and did in open_dids:
                    resolve_anomaly(did)
                    n_resolved += 1
                continue

            # A NOT-yet-open disagreement on a very fresh feed event is often
            # a legit change still propagating to the task record. Hold it for
            # the in-run recheck below (alarm ASAP, 2026-08-20) instead of
            # punting to the next run.
            if (did not in open_dids and d["last_event_at"] is not None
                    and started - d["last_event_at"] < FRESH_EVENT_WINDOW):
                logger.info(f"recheck-hold {did}: feed event "
                            f"{started - d['last_event_at']} old ({verdict})")
                recheck[did] = (rec, stored, verdict, d)
                continue

            record_anomaly(did, verdict, rec, stored, d)

        # In-run recheck: give Swift RECHECK_DELAY to finish propagating,
        # re-pull the report, re-classify. Still inconsistent -> real anomaly,
        # alert in THIS run. Cleared -> it was propagation, stay silent.
        if recheck:
            logger.info(f"rechecking {len(recheck)} fresh-event disagreement(s) "
                        f"after {int(RECHECK_DELAY.total_seconds())}s")
            time.sleep(RECHECK_DELAY.total_seconds())
            report2 = fetch_report_rows()
            # A truncated second pull must not silently swallow alerts:
            # confirm with first-pass data instead of "clearing" on absence.
            degraded = len(report2) < 0.7 * len(report)
            if degraded:
                logger.warning("recheck report pull looks truncated; "
                               "confirming with first-pass data")
            for did, (rec, stored, verdict, d) in recheck.items():
                if degraded:
                    record_anomaly(did, verdict, rec, stored, d)
                    continue
                rec2 = report2.get(did)
                stored2 = parse_scheduled(rec2) if rec2 else None
                if stored2 is None:
                    logger.info(f"recheck-clear {did}: no longer scheduled on record")
                    continue
                verdict2, d2 = classify(did, stored2)
                if verdict2 in ("ok", "fetch_error"):
                    logger.info(f"recheck-clear {did}: now {verdict2}")
                    continue
                record_anomaly(did, verdict2, rec2, stored2, d2)

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
                team = notice_team(db)
                for v, r, s, d in new_alerts:
                    sched_email = resolve_scheduler_email(db, d["last_event_by"])
                    notified = ""
                    if v in ("timed_mismatch", "ghost_schedule"):
                        if v == "ghost_schedule":
                            # Ghost notices go to Jamil ONLY for now
                            # (2026-08-20): he confirms them with the team
                            # before the audience widens. timed_mismatch keeps
                            # the full DA+PA+scheduler audience.
                            recips = list(ALERT_RECIPIENTS)
                        else:
                            recips = list(team)
                            if sched_email and sched_email not in recips:
                                recips.append(sched_email)
                        if args.notify_schedulers:
                            subject = (f"Schedule needs a quick re-do: "
                                       f"{r.get('Task Name')} - {r.get('Asset Name')}")
                            res = send_alert(
                                subject,
                                scheduler_notice_html(v, r, s, d),
                                recipients=recips,
                                sender_name="Ontel Schedule Check")
                            if res:
                                # Remember the thread so the resolution
                                # follow-up replies on it.
                                db.execute(
                                    "UPDATE pipeline.schedule_audit_anomalies SET "
                                    "notice_thread_id = $2, notice_message_id = $3, "
                                    "notice_subject = $4, notice_recipients = $5, "
                                    "notice_sent_at = now() WHERE task_did = $1",
                                    r.get("Task DID"), res["thread_id"],
                                    res["message_id"], subject, recips)
                            notified = f" - notice sent to: {', '.join(recips)}"
                        else:
                            notified = (f" - would send notice to: {', '.join(recips)} "
                                        f"(enable --notify-schedulers)")
                    if d["last_event_by"] and not sched_email:
                        notified += (f" - scheduler '{d['last_event_by']}' not uniquely "
                                     f"matched in directory; manual follow-up")
                    if v == "ghost_schedule":
                        # feed_scheduled on a ghost is the REMOVED value
                        # (p_schedule of the remove event) - phrasing it as
                        # "activity feed says X" read like a live mismatch and
                        # confused the reader when both values matched.
                        compare = (
                            f"task record still says <b>{_fmt_et(s)}</b>, but the "
                            f"feed shows the schedule was <b>removed</b>"
                            + (f" (removed value: {_fmt_et(d['feed_scheduled'])})"
                               if d["feed_scheduled"] else ""))
                    else:
                        compare = (f"task record says <b>{_fmt_et(s)}</b>, "
                                   f"activity feed says <b>{_fmt_et(d['feed_scheduled'])}</b>")
                    items.append(
                        f"<li><b>{r.get('Task Name')}</b> / {r.get('Asset Name')} "
                        f"({r.get('Project')})<br>"
                        f"class: {v} - {compare} "
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
            # All-clear follow-ups: reply on the original notice thread once
            # the anomaly resolves, so members know it's fixed.
            if args.notify_schedulers:
                for row in all_clear:
                    res = send_alert(
                        f"Re: {row['notice_subject']}",
                        all_clear_html(row),
                        recipients=list(row["notice_recipients"] or []) or None,
                        sender_name="Ontel Schedule Check",
                        thread_id=row["notice_thread_id"],
                        in_reply_to=row["notice_message_id"])
                    if res:
                        db.execute(
                            "UPDATE pipeline.schedule_audit_anomalies SET "
                            "resolved_notice_sent_at = now() WHERE task_did = $1",
                            row["task_did"])
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
