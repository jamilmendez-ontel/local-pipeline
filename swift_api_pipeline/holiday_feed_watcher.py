#!/usr/bin/env python3
"""Holiday feed watcher: detect new or amended Philippine holiday proclamations
and email Jamil the proposed reference.ref_holidays changes.

Sources:
  1. Official Gazette RSS (officialgazette.gov.ph/feed/?paged=N). Every
     proclamation's subject line is in <description>, e.g.
     "DECLARING WEDNESDAY, 27 MAY 2026, A REGULAR HOLIDAY THROUGHOUT THE
     COUNTRY, IN OBSERVANCE OF THE EID'L ADHA (FEAST OF SACRIFICE)".
     Pages are walked newest-first until the (year, number) watermark stored by
     the last real run is reached (cap --max-pages). Locality-only days
     ("... IN THE MUNICIPALITY OF ...") are ignored. Nationwide declarations,
     the annual list ("... REGULAR HOLIDAYS AND SPECIAL (NON-WORKING) DAYS FOR
     THE YEAR 2027") and amendments are compared against ref_holidays: a date we
     do not have -> proposed INSERT; a date whose type changed -> proposed
     UPDATE (special -> regular is the case that matters for pay rules).
  2. Nager.Date (date.nager.at/api/v3/PublicHolidays/{year}/PH): dates-only
     cross-check for this year and next. It carries no regular/special
     distinction, so a Nager-only date is a "verify" finding, never SQL.
  3. Staleness: the latest PH row must be at least HORIZON_DAYS ahead of today,
     otherwise next year's list has not been seeded yet.

The watcher NEVER writes reference.ref_holidays. Every finding carries the SQL
to run once the proclamation text is confirmed; a human applies it as a
migration. Run rows go to pipeline.holiday_watch_runs (migration 245); the
watermark is the highest (year, proclamation number) seen by a non-dry run.

Behavior: nothing new = one log line, run row status 'ok', no email, exit 0.
Findings = one plain-text email to Jamil, status 'findings', exit 0. Crash =
status 'error' when the DB is reachable, exit 1 (GitHub's workflow-failure
notification covers watcher self-death).

Usage:
    python holiday_feed_watcher.py               # check + email findings
    python holiday_feed_watcher.py --dry-run     # check + print; no email, watermark not advanced
    python holiday_feed_watcher.py --since 1300  # override the watermark (proclamation number, current year)
    python holiday_feed_watcher.py --max-pages 10
"""

import argparse
import base64
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

from config import get_logger, get_db, close_db, retry_db, setup_logging

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

setup_logging()
logger = get_logger("holiday_feed_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
GAZETTE_FEED = "https://www.officialgazette.gov.ph/feed/"
NAGER_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/PH"
HTTP_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; ontel-holiday-feed-watcher/1.0)"
DEFAULT_MAX_PAGES = 6       # first run / no watermark: how far back to look (~10 items per page)
HARD_MAX_PAGES = 30
HORIZON_DAYS = 120          # latest PH holiday must be at least this far ahead

MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
    r"OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})\b")
TITLE_RE = re.compile(r"Proclamation No\.?\s*(\d+)\s*,?\s*s\.?\s*(\d{4})", re.I)
PROC_RE = re.compile(r"PROCLAMATION NO\.?\s*(\d+)")
AMEND_RE = re.compile(r"AMENDING PROCLAMATION NO\.?\s*(\d+)")
ANNUAL_RE = re.compile(r"REGULAR HOLIDAYS AND SPECIAL \(NON-WORKING\) DAYS FOR THE YEAR (\d{4})")
LOCAL_RE = re.compile(
    r"\bIN THE (CITY|CITIES|MUNICIPALITY|MUNICIPALITIES|PROVINCE|PROVINCES|ISLAND|REGION|"
    r"BANGSAMORO|CORDILLERA|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH)\b|\bDISTRICT OF\b")
NATIONWIDE_RE = re.compile(r"THROUGHOUT THE (COUNTRY|PHILIPPINES)|\bNATIONWIDE\b")
OBSERVANCE_RE = re.compile(
    r"IN (?:OBSERVANCE|CELEBRATION|COMMEMORATION) OF (?:THE )?(.+?)(?:\s*\(|,|$)")
TYPE_LABEL = {
    "regular": "regular holiday",
    "special_non_working": "special (non-working) day",
    "special_working": "special (working) day",
}


# ---------------------------------------------------------------------------
# Pure evaluators (unit-tested; no network, no DB)
# ---------------------------------------------------------------------------

def _clean(text):
    text = (text or "").replace("&#160;", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_gazette_feed(xml_text):
    """RSS text -> list of proclamation items (newest first, as published).
    Non-proclamation issuances (EOs, AOs, memoranda) are skipped."""
    items = []
    # stdlib XML guard: an RSS feed never needs a DTD; refuse entity tricks
    # (billion laughs / XXE) instead of pulling in defusedxml.
    if re.search(r"<!(DOCTYPE|ENTITY)", xml_text[:4096], re.I):
        raise ValueError("feed contains a DTD/entity declaration; refusing to parse")
    root = ET.fromstring(xml_text)
    for node in root.iter("item"):
        title = _clean(node.findtext("title"))
        m = TITLE_RE.search(title)
        if not m:
            continue
        items.append({
            "proc_no": int(m.group(1)),
            "year": int(m.group(2)),
            "title": title,
            "link": _clean(node.findtext("link")),
            "subject": _clean(node.findtext("description")),
            "published": _clean(node.findtext("pubDate")),
        })
    return items


def classify_subject(subject):
    """Subject line -> what it declares. kind: annual_list | holiday_declaration |
    other. scope: nationwide | local | unknown (declarations only)."""
    s = _clean(subject).upper()
    out = {"kind": "other", "holiday_type": None, "dates": [], "scope": None,
           "annual_year": None, "amends": None, "name": None}
    m = ANNUAL_RE.search(s)
    if m:
        out.update(kind="annual_list", annual_year=int(m.group(1)), scope="nationwide")
        return out
    if "HOLIDAY" not in s and "NON-WORKING" not in s and "WORKING) DAY" not in s:
        return out
    if "REGULAR HOLIDAY" in s:
        out["holiday_type"] = "regular"
    elif "SPECIAL (NON-WORKING)" in s or "SPECIAL NON-WORKING" in s:
        out["holiday_type"] = "special_non_working"
    elif "SPECIAL (WORKING)" in s or "SPECIAL WORKING" in s:
        out["holiday_type"] = "special_working"
    dates = set()
    for d, mo, y in DATE_RE.findall(s):
        try:
            dates.add(date(int(y), MONTHS[mo], int(d)).isoformat())
        except ValueError:
            pass
    out["dates"] = sorted(dates)
    m = AMEND_RE.search(s)
    if m:
        out["amends"] = int(m.group(1))
    if NATIONWIDE_RE.search(s):
        out["scope"] = "nationwide"
    elif LOCAL_RE.search(s):
        out["scope"] = "local"
    else:
        out["scope"] = "unknown"
    m = OBSERVANCE_RE.search(s)
    if m:
        # title-case on word starts only ("EID'L ADHA" -> "Eid'l Adha", not "Eid'L")
        out["name"] = re.sub(r"(^|[\s(/-])([a-z])", lambda w: w.group(1) + w.group(2).upper(),
                             m.group(1).strip().lower())
    out["kind"] = "holiday_declaration"
    return out


def _proc_ref(item):
    return f"Proclamation No. {item['proc_no']}, s. {item['year']}"


def _sql_str(v):
    return "'" + str(v).replace("'", "''") + "'"


def sql_insert(iso_date, holiday_type, name, item):
    return (
        "INSERT INTO reference.ref_holidays (calendar, holiday_date, holiday_type, name, "
        "is_non_working, proclamation_ref, source) VALUES ('PH', "
        f"{_sql_str(iso_date)}, {_sql_str(holiday_type)}, {_sql_str(name)}, "
        f"{'true' if holiday_type != 'special_working' else 'false'}, "
        f"{_sql_str(_proc_ref(item))}, {_sql_str(item['link'])});"
    )


def sql_update(iso_date, holiday_type, item):
    return (
        "UPDATE reference.ref_holidays SET "
        f"holiday_type = {_sql_str(holiday_type)}, "
        f"is_non_working = {'true' if holiday_type != 'special_working' else 'false'}, "
        f"amended_by = {_sql_str(_proc_ref(item))}, "
        f"source = {_sql_str(item['link'])} "
        f"WHERE calendar = 'PH' AND holiday_date = {_sql_str(iso_date)};"
    )


def evaluate_gazette(items, db_rows, watermark):
    """items: parse_gazette_feed output. db_rows: {iso_date: {holiday_type, name,
    proclamation_ref}} for calendar PH. watermark: (year, proc_no) or None.
    Returns (findings, new_watermark). Items at or below the watermark are
    already reviewed and produce nothing."""
    findings = []
    new_wm = watermark
    for it in items:
        key = (it["year"], it["proc_no"])
        if new_wm is None or key > new_wm:
            new_wm = key
        if watermark is not None and key <= watermark:
            continue
        c = classify_subject(it["subject"])
        base = {"proc": _proc_ref(it), "link": it["link"], "subject": it["subject"]}
        if c["kind"] == "annual_list":
            findings.append({**base, "kind": "annual_list", "year": c["annual_year"],
                             "note": f"Annual list for {c['annual_year']} published. Seed it "
                                     f"as one migration block (see 244_ref_holidays.sql)."})
            continue
        if c["kind"] != "holiday_declaration" or c["scope"] == "local":
            continue
        if c["scope"] == "unknown" or not c["dates"] or not c["holiday_type"]:
            findings.append({**base, "kind": "review",
                             "note": "Holiday wording but scope/date/type not parseable; read it."})
            continue
        for iso in c["dates"]:
            have = db_rows.get(iso)
            name = c["name"] or "(name from proclamation)"
            if have is None:
                findings.append({**base, "kind": "new_nationwide", "date": iso,
                                 "holiday_type": c["holiday_type"], "name": name,
                                 "sql": sql_insert(iso, c["holiday_type"], name, it)})
            elif have["holiday_type"] != c["holiday_type"]:
                findings.append({**base, "kind": "type_change", "date": iso,
                                 "holiday_type": c["holiday_type"], "name": have["name"],
                                 "previous_type": have["holiday_type"],
                                 "sql": sql_update(iso, c["holiday_type"], it)})
            # else: already known with the same type; nothing to do
    return findings, new_wm


def evaluate_nager(nager_rows, db_rows, today, already_reported=frozenset()):
    """nager_rows: list of {date, name, ...} (Nager.Date). Future dates Nager
    lists that ref_holidays lacks -> verify findings (no SQL: no type info).
    Only years already seeded in ref_holidays are compared: Nager publishes
    projected dates for next year long before any proclamation exists, and the
    staleness check is what flags an unseeded year. Dates in already_reported
    (emailed by an earlier run) are not repeated."""
    findings = []
    seen = set()
    seeded_years = {iso[:4] for iso in db_rows}
    for r in nager_rows:
        iso = r.get("date")
        if (not iso or iso in seen or iso < today.isoformat() or iso in db_rows
                or iso[:4] not in seeded_years or iso in already_reported):
            continue
        seen.add(iso)
        findings.append({"kind": "nager_only", "date": iso, "name": r.get("name") or "",
                         "note": "Nager.Date lists this date; ref_holidays does not. "
                                 "Verify against a proclamation before adding (Nager "
                                 "over-includes and carries no regular/special type)."})
    return findings


def evaluate_staleness(db_rows, today, horizon_days=HORIZON_DAYS):
    if not db_rows:
        return [{"kind": "stale", "note": "ref_holidays has no PH rows at all."}]
    latest = max(db_rows)
    days_ahead = (date.fromisoformat(latest) - today).days
    if days_ahead < horizon_days:
        return [{"kind": "stale",
                 "note": f"Latest PH holiday on file is {latest} ({days_ahead} days ahead, "
                         f"threshold {horizon_days}). Next year's proclamation list is "
                         f"probably out or due; seed it."}]
    return []


def _dow(iso):
    return date.fromisoformat(iso).strftime("%a")


def build_email_body(findings, checked_at, stats):
    et = checked_at.astimezone(ZoneInfo("America/New_York"))
    lines = [f"Holiday feed watch: {len(findings)} finding(s)  "
             f"(checked {et.strftime('%Y-%m-%d %H:%M')} ET)", ""]
    for i, f in enumerate(findings, 1):
        k = f["kind"]
        if k == "new_nationwide":
            lines.append(f"{i}. NEW nationwide declaration - {f['proc']}")
            lines.append(f"   {f['date']} ({_dow(f['date'])})  {TYPE_LABEL[f['holiday_type']]}  \"{f['name']}\"")
        elif k == "type_change":
            lines.append(f"{i}. TYPE CHANGE - {f['proc']}")
            lines.append(f"   {f['date']} ({_dow(f['date'])})  \"{f['name']}\": "
                         f"{TYPE_LABEL[f['previous_type']]} -> {TYPE_LABEL[f['holiday_type']]}")
        elif k == "annual_list":
            lines.append(f"{i}. ANNUAL LIST for {f['year']} - {f['proc']}")
            lines.append(f"   {f['note']}")
        elif k == "review":
            lines.append(f"{i}. REVIEW - {f['proc']}")
            lines.append(f"   {f['note']}")
        elif k == "nager_only":
            lines.append(f"{i}. VERIFY - {f['date']} ({_dow(f['date'])}) \"{f['name']}\"")
            lines.append(f"   {f['note']}")
        elif k == "stale":
            lines.append(f"{i}. STALE")
            lines.append(f"   {f['note']}")
        if f.get("subject"):
            lines.append(f"   Subject: {f['subject']}")
        if f.get("link"):
            lines.append(f"   Link: {f['link']}")
        if f.get("sql"):
            lines.append("   SQL (after confirming the proclamation text):")
            lines.append(f"     {f['sql']}")
        lines.append("")
    lines.append(
        f"Scanned: {stats.get('pages', 0)} Official Gazette page(s), "
        f"{stats.get('items', 0)} proclamation(s), watermark "
        f"{stats.get('watermark_before') or 'none'} -> {stats.get('watermark_after') or 'none'}; "
        f"Nager.Date {stats.get('nager_years', '')}: {stats.get('nager_dates', 0)} date(s).")
    lines.append("Runbook: confirm each item against the proclamation text, put the SQL in the "
                 "next free local-pipeline migration, apply, reply done. The watcher never "
                 "edits ref_holidays itself.")
    lines.append("")
    lines.append("(holiday_feed_watcher.py; silent when nothing new)")
    return "\n".join(lines)


def fmt_watermark(wm):
    return f"{wm[1]} s.{wm[0]}" if wm else None


# ---------------------------------------------------------------------------
# Probes (thin; network + DB)
# ---------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_gazette(session, watermark, max_pages):
    """Walk feed pages newest-first. Stop after the page that reaches the
    watermark (or after max_pages / an empty page). Returns (items, pages)."""
    items, pages = [], 0
    for page in range(1, max_pages + 1):
        url = GAZETTE_FEED if page == 1 else f"{GAZETTE_FEED}?paged={page}"
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:      # past the last page
            break
        resp.raise_for_status()
        page_items = parse_gazette_feed(resp.text)
        pages += 1
        if not page_items:
            break
        items.extend(page_items)
        if watermark is not None and any(
                (it["year"], it["proc_no"]) <= watermark for it in page_items):
            break
    return items, pages


def fetch_nager(session, year):
    resp = session.get(NAGER_URL.format(year=year), timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:          # year not published yet
        return []
    resp.raise_for_status()
    return resp.json()


def probe_holidays(db):
    rows = retry_db(lambda: db.fetch(
        "SELECT holiday_date, holiday_type, name, proclamation_ref "
        "FROM reference.ref_holidays WHERE calendar = 'PH'"),
        description="read ref_holidays")
    return {r["holiday_date"].isoformat(): {
        "holiday_type": r["holiday_type"], "name": r["name"],
        "proclamation_ref": r["proclamation_ref"]} for r in rows}


def probe_watermark(db):
    row = retry_db(lambda: db.fetchrow(
        "SELECT og_watermark, og_watermark_year FROM pipeline.holiday_watch_runs "
        "WHERE NOT dry_run AND status <> 'error' AND og_watermark IS NOT NULL "
        "ORDER BY og_watermark_year DESC, og_watermark DESC LIMIT 1"),
        description="read watermark")
    return (row["og_watermark_year"], row["og_watermark"]) if row else None


def record_run(db, status, stats, findings, emailed, dry_run, error=None):
    wm = stats.get("wm_after")
    retry_db(lambda: db.execute(
        "INSERT INTO pipeline.holiday_watch_runs (status, og_items_scanned, og_watermark, "
        "og_watermark_year, nager_dates, findings, emailed, error, dry_run) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        status, stats.get("items", 0), wm[1] if wm else None, wm[0] if wm else None,
        stats.get("nager_dates", 0), json.loads(json.dumps(findings, default=str)), emailed,
        error, dry_run), description="record run")   # db.py's jsonb codec json.dumps() the object itself


def probe_reported_nager(db):
    """Dates already emailed as nager_only by a real run: report each once, not
    weekly forever (a date that was verified and added lands in ref_holidays
    and drops out anyway; one judged noise stays quiet)."""
    rows = retry_db(lambda: db.fetch(
        "SELECT DISTINCT f->>'date' AS d FROM pipeline.holiday_watch_runs r, "
        "jsonb_array_elements(r.findings) f "
        "WHERE NOT r.dry_run AND r.emailed AND f->>'kind' = 'nager_only'"),
        description="read reported nager dates")
    return {r["d"] for r in rows if r["d"]}


# ---------------------------------------------------------------------------
# Email (same Gmail API pattern as pipeline_health_watcher)
# ---------------------------------------------------------------------------

def send_email(findings, checked_at, stats):
    import gmail_client
    from gmail_client import authenticate, masked_sender
    if not gmail_client.TOKEN_FILE.exists():
        raise RuntimeError(f"gmail token missing ({gmail_client.TOKEN_FILE}); "
                           f"refusing interactive OAuth in a headless job")
    service = authenticate()
    msg = MIMEText(build_email_body(findings, checked_at, stats), "plain")
    msg["To"] = ", ".join(RECIPIENTS)
    msg["From"] = masked_sender(service, "Pipeline Alerts")
    msg["Subject"] = f"Holiday feed watch: {len(findings)} finding(s)"
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Findings email sent to {', '.join(RECIPIENTS)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print findings; no email; watermark not advanced")
    parser.add_argument("--since", type=int, default=None,
                        help="override watermark: proclamation number (current year)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help=f"feed pages to walk (default {DEFAULT_MAX_PAGES} with no "
                             f"watermark, {HARD_MAX_PAGES} cap when walking to one)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Asia/Manila")).date()
    stats = {}
    findings = []
    db = None
    try:
        db = get_db()
        db_rows = probe_holidays(db)
        watermark = probe_watermark(db)
        if args.since is not None:
            watermark = (today.year, args.since)
        max_pages = args.max_pages or (HARD_MAX_PAGES if watermark else DEFAULT_MAX_PAGES)
        logger.info(f"ref_holidays PH rows: {len(db_rows)}; watermark: {fmt_watermark(watermark)}; "
                    f"max pages: {max_pages}")

        session = make_session()
        items, pages = fetch_gazette(session, watermark, max_pages)
        gz_findings, wm_after = evaluate_gazette(items, db_rows, watermark)
        stats.update(pages=pages, items=len(items), watermark_before=fmt_watermark(watermark),
                     watermark_after=fmt_watermark(wm_after), wm_after=wm_after)
        logger.info(f"Gazette: {pages} page(s), {len(items)} proclamation(s), "
                    f"{len(gz_findings)} finding(s); watermark -> {fmt_watermark(wm_after)}")
        if watermark and items and pages >= max_pages and all(
                (it["year"], it["proc_no"]) > watermark for it in items):
            logger.warning(f"Walked {pages} pages without reaching watermark "
                           f"{fmt_watermark(watermark)}; older items may be unreviewed.")

        nager_rows = []
        for y in (today.year, today.year + 1):
            try:
                nager_rows += fetch_nager(session, y)
            except requests.RequestException as e:
                logger.warning(f"Nager.Date {y} unavailable: {e}")
        stats.update(nager_years=f"{today.year}+{today.year + 1}", nager_dates=len(nager_rows))

        findings = gz_findings
        findings += evaluate_nager(nager_rows, db_rows, today, probe_reported_nager(db))
        findings += evaluate_staleness(db_rows, today)

        if not findings:
            logger.info("Nothing new in the holiday feeds; no email.")
            record_run(db, "ok", stats, [], False, args.dry_run)
            return 0

        logger.warning(f"{len(findings)} finding(s):")
        for f in findings:
            logger.warning(f"  - {f['kind']}: {f.get('date') or f.get('year') or ''} "
                           f"{f.get('proc') or ''} {f.get('note') or f.get('name') or ''}")
        emailed = False
        if args.dry_run:
            logger.info("Dry run: email suppressed.\n" + build_email_body(findings, now, stats))
        else:
            send_email(findings, now, stats)
            emailed = True
        record_run(db, "findings", stats, findings, emailed, args.dry_run)
        return 0
    except Exception as e:
        logger.exception("holiday feed watcher crashed")
        if db is not None:
            try:
                record_run(db, "error", stats, findings, False, args.dry_run, error=str(e)[:2000])
            except Exception:
                logger.exception("could not record the error run row")
        return 1
    finally:
        if db is not None:
            close_db()


if __name__ == "__main__":
    sys.exit(main())
