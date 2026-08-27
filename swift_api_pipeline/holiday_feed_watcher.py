#!/usr/bin/env python3
"""Holiday feed watcher: detect new or amended Philippine holiday proclamations
and email Jamil the proposed reference.ref_holidays changes.

Sources:
  1. lawphil.net per-year proclamation index (PRIMARY; plain nginx, reachable
     from GitHub runners). One table row per proclamation: number, signing
     date and the full title, e.g. "Declaring Wednesday, 27 May 2026, a Regular
     Holiday Throughout the Country, in Observance of the Eid'l Adha". The
     whole year page (plus last year's in Jan-Feb: the annual list for year N
     is proclaimed in Sep of N-1) is scanned every run; a proclamation is new
     when its key ("year:number") is not in any earlier real run's scanned
     set (pipeline.holiday_watch_runs.scanned_keys). lawphil lags the Gazette
     by roughly two weeks.
  2. Official Gazette RSS (officialgazette.gov.ph/feed/?paged=N), BEST
     EFFORT: fresher than lawphil, but Cloudflare in front of gov.ph returns
     403 to GitHub runner IPs (probed 2026-08-27, any User-Agent), so a 403 is
     a warning, not a failure. When reachable (local runs) it is walked
     newest-first back to the last run's coverage_ts minus a slack window
     (the feed is ordered by publish time, NOT by number: 1404 was posted
     after 1409) and merged with the lawphil items by key.
     Locality-only days ("... IN THE MUNICIPALITY OF ...") are ignored;
     nationwide declarations, the annual list ("... REGULAR HOLIDAYS AND
     SPECIAL (NON-WORKING) DAYS FOR THE YEAR 2027") and amendments are compared
     against ref_holidays: a date we do not have -> proposed INSERT; a date
     whose type changed -> proposed UPDATE (special -> regular is the case
     that matters for pay rules).
  3. Nager.Date (date.nager.at/api/v3/PublicHolidays/{year}/PH): dates-only
     cross-check for years already seeded. It carries no regular/special
     distinction, so a Nager-only date is a "verify" finding, never SQL, and
     each date is reported once.
  4. Staleness: the latest PH row must be at least HORIZON_DAYS ahead of today,
     otherwise next year's list has not been seeded yet (reported at most once
     a week even if the job fires twice).

The watcher NEVER writes reference.ref_holidays. Every finding carries the SQL
to run once the proclamation text is confirmed; a human applies it as a
migration. Run rows go to pipeline.holiday_watch_runs (migrations 245/246).
Coverage advances only on a real (non-dry) run that recorded an email or had
nothing to say; an error run (feed down, DB down after start, Gmail failure)
never advances it, so findings are re-sent next week rather than lost.

Behavior: nothing new = one log line, run row status 'ok', no email, exit 0.
Findings = one plain-text email to Jamil, status 'findings', exit 0. Crash =
status 'error' when the DB is reachable, exit 1 (GitHub's workflow-failure
notification covers watcher self-death).

Usage:
    python holiday_feed_watcher.py                    # check + email findings
    python holiday_feed_watcher.py --dry-run          # check + print; no email, coverage not advanced
    python holiday_feed_watcher.py --lookback-days 60 # walk back this far instead of the recorded coverage
    python holiday_feed_watcher.py --max-pages 10
"""

import argparse
import base64
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

from config import get_logger, get_db, close_db, retry_db, setup_logging

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

setup_logging()
logger = get_logger("holiday_feed_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
LAWPHIL_INDEX = "https://lawphil.net/executive/proc/proc{year}/proc{year}.html"
LAWPHIL_ITEM = "https://lawphil.net/executive/proc/proc{year}/proc_{no}_{year}.html"
GAZETTE_FEED = "https://www.officialgazette.gov.ph/feed/"
NAGER_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/PH"
HTTP_TIMEOUT = 30
# A plain browser UA: the Gazette's WAF returned 403 to a self-identifying bot UA from
# the GitHub runner on the first dispatched run (2026-08-27) while the same UA worked
# from a residential IP.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0.0.0 Safari/537.36")
BOT_USER_AGENT = "Mozilla/5.0 (compatible; ontel-holiday-feed-watcher/1.0)"
PROBE_URLS = [
    LAWPHIL_INDEX.format(year=2026),
    GAZETTE_FEED,
    GAZETTE_FEED + "?paged=2",
    "https://www.officialgazette.gov.ph/?feed=rss2",
    "https://www.officialgazette.gov.ph/",
    "https://pco.gov.ph/news_releases/feed/",
    NAGER_URL.format(year=2026),
]
LAWPHIL_ROW_RE = re.compile(
    r'<a href="proc_(\d+)_(\d{4})\.html">\s*Proclamation No\.?\s*\d+\s*</a>\s*<br\s*/?>\s*'
    r'([A-Za-z]+\s+\d{1,2},\s*\d{4})\s*</td>\s*<td>\s*(.*?)(?:<a class=tenure>|</td>)',
    re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DEFAULT_LOOKBACK_DAYS = 30  # first run / no coverage yet: how far back to look
SLACK_DAYS = 21             # re-walk this far behind the last coverage point
DEFAULT_MAX_PAGES = 30      # ~10 posts per page; 30 pages ~ 3 months of Gazette output
SEEN_KEYS_DAYS = 180        # scanned-key memory window (walk never goes further back)
HORIZON_DAYS = 120          # latest PH holiday must be at least this far ahead
STALE_REPEAT_DAYS = 6       # STALE reported at most once per this many days
DATE_SANITY_PAST_DAYS = 365
DATE_SANITY_FUTURE_DAYS = 730

MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
    r"OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})\b")
TITLE_RE = re.compile(r"Proclamation No\.?\s*(\d+)\s*,?\s*s\.?\s*(\d{4})", re.I)
AMEND_RE = re.compile(r"AMENDING PROCLAMATION NO\.?\s*(\d+)")
ANNUAL_RE = re.compile(r"REGULAR HOLIDAYS AND SPECIAL \(NON-WORKING\) DAYS FOR THE YEAR (\d{4})")
LOCAL_RE = re.compile(
    r"\bIN THE (CITY|CITIES|MUNICIPALITY|MUNICIPALITIES|PROVINCE|PROVINCES|ISLAND|REGION|"
    r"BANGSAMORO|CORDILLERA|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH)\b|\bDISTRICT OF\b"
    r"|\bPROVINCE\b|\bMUNICI?PALIT(Y|IES)\b"                       # "in Mountain Province", lawphil typo
    r"|\b(DAYS?|HOLIDAYS?) IN (?!OBSERVANCE\b|CELEBRATION\b|COMMEMORATION\b)")  # "... DAY IN <place>"
NATIONWIDE_RE = re.compile(r"THROUGHOUT THE (COUNTRY|PHILIPPINES)|\bNATIONWIDE\b")
OBSERVANCE_RE = re.compile(
    r"IN (?:OBSERVANCE|CELEBRATION|COMMEMORATION) OF (?:THE )?(.+?)(?:\s*\(|,|$)")
TYPE_PHRASES = [
    ("regular", re.compile(r"REGULAR HOLIDAY")),
    ("special_non_working", re.compile(r"SPECIAL \(?NON-WORKING\)?")),
    ("special_working", re.compile(r"SPECIAL \(?WORKING\)? (?:DAY|HOLIDAY)")),
]
TYPE_LABEL = {
    "regular": "regular holiday",
    "special_non_working": "special (non-working) day",
    "special_working": "special (working) day",
}


# ---------------------------------------------------------------------------
# Pure evaluators (unit-tested; no network, no DB)
# ---------------------------------------------------------------------------

def _clean(text):
    text = (text or "").replace("&#160;", " ").replace("\xa0", " ").replace("&amp;", "&")
    text = text.replace("’", "'").replace("‘", "'").replace("\x92", "'")
    return re.sub(r"\s+", " ", text).strip()


def _pubdate(text):
    try:
        dt = parsedate_to_datetime(_clean(text))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def item_key(it):
    return f"{it['year']}:{it['proc_no']}"


def parse_feed_page(xml_text):
    """RSS text -> (proclamation items newest first, raw <item> pubdates).
    The raw list tells a page full of EOs/AOs (no proclamations) apart from
    the true end of the feed and drives the publish-time stop condition.
    Raises ValueError when the body is not an RSS document (Cloudflare
    challenge, maintenance page, feed moved) so the run fails loudly instead
    of reporting "nothing new"."""
    text = xml_text.lstrip("﻿ \r\n\t")   # BOM / leading whitespace (WordPress plugin defect)
    head = text[:512].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise ValueError("Official Gazette returned HTML, not RSS: " + _clean(text[:160]))
    # stdlib XML guard: an RSS feed never needs a DTD; refuse entity tricks
    # (billion laughs / XXE) instead of pulling in defusedxml.
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise ValueError("feed contains a DTD/entity declaration; refusing to parse")
    root = ET.fromstring(text)
    if root.tag.lower() != "rss":
        raise ValueError(f"Official Gazette body is not RSS (root <{root.tag}>)")
    items, raw_pubs = [], []
    for node in root.iter("item"):
        pub = _pubdate(node.findtext("pubDate"))
        raw_pubs.append(pub)
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
            "published": pub,
        })
    return items, raw_pubs


def parse_gazette_feed(xml_text):
    """RSS text -> list of proclamation items (newest first, as published)."""
    return parse_feed_page(xml_text)[0]


def parse_lawphil_index(html, year):
    """lawphil per-year index HTML -> proclamation items (page order, newest
    first). Raises ValueError when no rows parse: the page moved or is not the
    index, and "nothing new" must not be the silent outcome."""
    items = []
    for no, yr, signed, title in LAWPHIL_ROW_RE.findall(html):
        if int(yr) != year:
            continue
        try:
            published = datetime.strptime(_clean(signed), "%B %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            published = None
        items.append({
            "proc_no": int(no), "year": int(yr),
            "title": f"Proclamation No. {int(no)} s. {yr}",
            "link": LAWPHIL_ITEM.format(year=yr, no=int(no)),
            "subject": _clean(TAG_RE.sub(" ", title)),
            "published": published,
        })
    if not items:
        raise ValueError(f"lawphil {year} index parsed 0 proclamation rows: " + _clean(html[:160]))
    return items


def merge_items(*lists):
    """Union of item lists by key; the first occurrence wins (pass the primary
    source first). Order: newest key first."""
    seen, out = set(), []
    for lst in lists:
        for it in lst:
            k = item_key(it)
            if k not in seen:
                seen.add(k)
                out.append(it)
    out.sort(key=lambda it: (it["year"], it["proc_no"]), reverse=True)
    return out


def classify_subject(subject):
    """Subject line -> what it declares. kind: annual_list | holiday_declaration |
    other. scope: nationwide | local | unknown (declarations only).
    holiday_type is None when the subject names more than one type (an
    amendment quoting the old type, or a two-date mixed declaration): such
    items become "review" findings, never SQL."""
    s = _clean(subject).upper()
    out = {"kind": "other", "holiday_type": None, "dates": [], "scope": None,
           "annual_year": None, "amends": None, "name": None}
    m = ANNUAL_RE.search(s)
    if m:
        out.update(kind="annual_list", annual_year=int(m.group(1)), scope="nationwide")
        return out
    if "HOLIDAY" not in s and "NON-WORKING" not in s and "WORKING) DAY" not in s:
        return out
    types = [t for t, rx in TYPE_PHRASES if rx.search(s)]
    out["holiday_type"] = types[0] if len(types) == 1 else None
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


def evaluate_gazette(items, db_rows, seen_keys, today):
    """items: parse_feed_page output. db_rows: {iso_date: {holiday_type, name,
    proclamation_ref}} for calendar PH. seen_keys: proclamation keys scanned by
    earlier real runs. Returns (findings, keys scanned this run). Items whose
    key is already in seen_keys were reviewed before and produce nothing."""
    findings = []
    scanned = []
    lo = (today - timedelta(days=DATE_SANITY_PAST_DAYS)).isoformat()
    hi = (today + timedelta(days=DATE_SANITY_FUTURE_DAYS)).isoformat()
    for it in items:
        key = item_key(it)
        scanned.append(key)
        if key in seen_keys:
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
                             "note": "Holiday wording but scope, date or a single type could "
                                     "not be parsed (amendments quoting the old type land "
                                     "here on purpose); read the proclamation."})
            continue
        if any(iso < lo or iso > hi for iso in c["dates"]):
            findings.append({**base, "kind": "review",
                             "note": f"Parsed date(s) {', '.join(c['dates'])} fall outside "
                                     f"{lo}..{hi}; read the proclamation."})
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
    return findings, scanned


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
    if not isinstance(nager_rows, list):
        return findings
    for r in nager_rows:
        if not isinstance(r, dict):
            continue
        iso = r.get("date")
        if (not isinstance(iso, str) or iso in seen or iso < today.isoformat() or iso in db_rows
                or iso[:4] not in seeded_years or iso in already_reported):
            continue
        seen.add(iso)
        findings.append({"kind": "nager_only", "date": iso, "name": str(r.get("name") or ""),
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


def coverage_gap_finding(pages, n_items, cutoff):
    return {"kind": "review", "proc": "(coverage gap)", "link": GAZETTE_FEED,
            "note": f"Walked {pages} page(s) ({n_items} proclamations) without getting back "
                    f"to {cutoff.date()}; coverage was NOT advanced past the oldest post "
                    f"seen. Re-run with a larger --max-pages, or review the gap by hand."}


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
        else:
            lines.append(f"{i}. {k.upper()}")
            lines.append(f"   {f.get('note', '')}")
        if f.get("subject"):
            lines.append(f"   Subject: {f['subject']}")
        if f.get("link"):
            lines.append(f"   Link: {f['link']}")
        if f.get("sql"):
            lines.append("   SQL (after confirming the proclamation text):")
            lines.append(f"     {f['sql']}")
        lines.append("")
    lines.append(
        f"Scanned: lawphil {stats.get('lawphil_years', '')}: {stats.get('lawphil_items', 0)} "
        f"proclamation(s); Official Gazette: {stats.get('gazette_note', 'not attempted')}; "
        f"{stats.get('items', 0)} distinct, {stats.get('new_items', 0)} not seen before; "
        f"Nager.Date {stats.get('nager_years', '')}: {stats.get('nager_dates', 0)} date(s).")
    lines.append("Runbook: confirm each item against the proclamation text, put the SQL in the "
                 "next free local-pipeline migration, apply, reply done. The watcher never "
                 "edits ref_holidays itself.")
    lines.append("")
    lines.append("(holiday_feed_watcher.py; silent when nothing new)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probes (thin; network + DB)
# ---------------------------------------------------------------------------

def make_session(user_agent=USER_AGENT):
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def probe_sources():
    """--probe: print status + body head for each candidate source under both
    user agents. No DB, no email. Used to diagnose runner-side blocking."""
    for ua_name, ua in (("browser", USER_AGENT), ("bot", BOT_USER_AGENT)):
        s = make_session(ua)
        for url in PROBE_URLS:
            try:
                r = s.get(url, timeout=HTTP_TIMEOUT)
                head = _clean(r.text[:120])
                print(f"[{ua_name}] {r.status_code} {url}  server={r.headers.get('server', '?')} "
                      f"cf-ray={r.headers.get('cf-ray', '-')}  {head}")
            except requests.RequestException as e:
                print(f"[{ua_name}] EXC {url}  {e}")


def _raise_for_status(resp, url):
    if resp.status_code >= 400:
        raise requests.HTTPError(
            f"{resp.status_code} for {url}: server={resp.headers.get('server', '?')} "
            f"cf-ray={resp.headers.get('cf-ray', '-')} body={_clean(resp.text[:200])!r}",
            response=resp)


def fetch_gazette(session, cutoff, max_pages):
    """Walk feed pages newest-first until every post on a page is older than
    cutoff (the feed is ordered by publish time). Returns (items, pages,
    reached, oldest_seen). reached is True only when coverage back to cutoff is
    provable: a page entirely older than cutoff, or the true end of the feed
    (404 / a page with no <item>). A page with no proclamations but other
    issuances does NOT end the walk. When reached is False the caller must not
    advance coverage past oldest_seen, or the gap is skipped forever."""
    items, pages, reached, oldest = [], 0, False, None
    for page in range(1, max_pages + 1):
        url = GAZETTE_FEED if page == 1 else f"{GAZETTE_FEED}?paged={page}"
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:      # past the last page
            reached = True
            break
        _raise_for_status(resp, url)
        page_items, raw_pubs = parse_feed_page(resp.text)
        pages += 1
        if not raw_pubs:                 # true end of the feed
            reached = True
            break
        items.extend(page_items)
        known = [p for p in raw_pubs if p is not None]
        if known:
            page_oldest = min(known)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            if all(p <= cutoff for p in known):
                reached = True
                break
    return items, pages, reached, oldest


def fetch_lawphil(session, year):
    url = LAWPHIL_INDEX.format(year=year)
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:          # year page not created yet (early January)
        return []
    _raise_for_status(resp, url)
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = "cp1252"         # lawphil serves windows-1252 without saying so
    return parse_lawphil_index(resp.text, year)


def fetch_nager(session, year):
    resp = session.get(NAGER_URL.format(year=year), timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:          # year not published yet
        return []
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def probe_holidays(db):
    rows = retry_db(lambda: db.fetch(
        "SELECT holiday_date, holiday_type, name, proclamation_ref "
        "FROM reference.ref_holidays WHERE calendar = 'PH'"),
        description="read ref_holidays")
    return {r["holiday_date"].isoformat(): {
        "holiday_type": r["holiday_type"], "name": r["name"],
        "proclamation_ref": r["proclamation_ref"]} for r in rows}


def probe_coverage(db):
    """(coverage_ts of the latest real run, set of keys scanned by real runs in
    the memory window). Error and dry runs never count."""
    row = retry_db(lambda: db.fetchrow(
        "SELECT max(coverage_ts) AS ts FROM pipeline.holiday_watch_runs "
        "WHERE NOT dry_run AND status <> 'error'"),
        description="read coverage")
    rows = retry_db(lambda: db.fetch(
        "SELECT DISTINCT k FROM pipeline.holiday_watch_runs r, "
        "jsonb_array_elements_text(r.scanned_keys) k "
        "WHERE NOT r.dry_run AND r.status <> 'error' "
        "AND r.ran_at > now() - make_interval(days => $1)", SEEN_KEYS_DAYS),
        description="read scanned keys")
    return (row["ts"] if row else None), {r["k"] for r in rows}


def probe_reported(db, kind, within_days=None):
    """Dates (or a bare count for kinds without a date) of findings of `kind`
    already emailed by real runs, optionally within the last N days."""
    sql = ("SELECT f->>'date' AS d FROM pipeline.holiday_watch_runs r, "
           "jsonb_array_elements(r.findings) f "
           "WHERE NOT r.dry_run AND r.emailed AND f->>'kind' = $1")
    args = [kind]
    if within_days is not None:
        sql += " AND r.ran_at > now() - make_interval(days => $2)"
        args.append(within_days)
    rows = retry_db(lambda: db.fetch(sql, *args), description=f"read reported {kind}")
    return [r["d"] for r in rows]


def record_run(db, status, stats, findings, emailed, dry_run, error=None):
    wm = stats.get("max_key")
    retry_db(lambda: db.execute(
        "INSERT INTO pipeline.holiday_watch_runs (status, og_items_scanned, og_watermark, "
        "og_watermark_year, nager_dates, findings, emailed, error, dry_run, scanned_keys, "
        "coverage_ts) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        status, stats.get("items", 0), wm[1] if wm else None, wm[0] if wm else None,
        stats.get("nager_dates", 0), json.loads(json.dumps(findings, default=str)), emailed,
        error, dry_run, list(stats.get("scanned", [])), stats.get("coverage_ts")),
        description="record run")   # db.py's jsonb codec json.dumps() the objects itself


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
    service.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=3)
    logger.info(f"Findings email sent to {', '.join(RECIPIENTS)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print findings; no email; coverage not advanced")
    parser.add_argument("--lookback-days", type=int, default=None,
                        help="walk back this many days instead of the recorded coverage")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"feed pages to walk at most (default {DEFAULT_MAX_PAGES})")
    parser.add_argument("--probe", action="store_true",
                        help="print HTTP status of every candidate source; no DB, no email")
    args = parser.parse_args()
    if args.probe:
        probe_sources()
        return 0

    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Asia/Manila")).date()
    stats = {}
    findings = []
    db = None
    try:
        db = get_db()
        db_rows = probe_holidays(db)
        coverage_ts, seen_keys = probe_coverage(db)
        if args.lookback_days is not None:
            cutoff = now - timedelta(days=args.lookback_days)
        elif coverage_ts is not None:
            cutoff = coverage_ts - timedelta(days=SLACK_DAYS)
        else:
            cutoff = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        cutoff = max(cutoff, now - timedelta(days=SEEN_KEYS_DAYS))
        logger.info(f"ref_holidays PH rows: {len(db_rows)}; coverage: {coverage_ts}; "
                    f"seen keys: {len(seen_keys)}; walking back to {cutoff.date()}; "
                    f"max pages: {args.max_pages}")

        session = make_session()

        # Primary: lawphil year index (current year; plus last year in Jan-Feb).
        lawphil_years = [today.year] + ([today.year - 1] if today.month <= 2 else [])
        lawphil_items = []
        for y in lawphil_years:
            lawphil_items += fetch_lawphil(session, y)   # raises -> error run, exit 1
        stats.update(lawphil_years="+".join(map(str, lawphil_years)),
                     lawphil_items=len(lawphil_items))
        logger.info(f"lawphil {stats['lawphil_years']}: {len(lawphil_items)} proclamation(s)")

        # Best effort: Official Gazette RSS (blocked for GitHub runner IPs).
        gz_items, gz_findings, new_coverage = [], [], coverage_ts
        try:
            gz_items, pages, reached, oldest = fetch_gazette(session, cutoff, args.max_pages)
            if reached:
                new_coverage = now
            else:
                gz_findings.append(coverage_gap_finding(pages, len(gz_items), cutoff))
                logger.warning(gz_findings[-1]["note"])
                new_coverage = oldest if oldest is not None else coverage_ts
            stats["gazette_note"] = f"{pages} page(s), {len(gz_items)} proclamation(s) back to {cutoff.date()}"
        except (requests.RequestException, ValueError) as e:
            blocked = isinstance(e, requests.HTTPError) and e.response is not None \
                and e.response.status_code == 403
            stats["gazette_note"] = ("blocked (403) from this network" if blocked
                                     else f"unavailable ({_clean(str(e))[:120]})")
            logger.warning(f"Official Gazette skipped: {stats['gazette_note']}")
        logger.info(f"Gazette: {stats['gazette_note']}")

        items = merge_items(lawphil_items, gz_items)
        findings_gz, scanned = evaluate_gazette(items, db_rows, seen_keys, today)
        gz_findings = findings_gz + gz_findings
        new_items = len([k for k in scanned if k not in seen_keys])
        max_key = max(((it["year"], it["proc_no"]) for it in items), default=None)
        stats.update(items=len(items), new_items=new_items, cutoff=cutoff.date(),
                     scanned=scanned, coverage_ts=new_coverage, max_key=max_key)
        logger.info(f"Proclamations: {len(items)} distinct, {new_items} new, "
                    f"{len(gz_findings)} finding(s); coverage -> {new_coverage}")

        nager_rows = []
        for y in (today.year, today.year + 1):
            try:
                nager_rows += fetch_nager(session, y)
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"Nager.Date {y} unavailable: {e}")
        stats.update(nager_years=f"{today.year}+{today.year + 1}", nager_dates=len(nager_rows))

        findings = gz_findings
        findings += evaluate_nager(nager_rows, db_rows, today,
                                   set(d for d in probe_reported(db, "nager_only") if d))
        stale = evaluate_staleness(db_rows, today)
        if stale and probe_reported(db, "stale", within_days=STALE_REPEAT_DAYS):
            logger.info("STALE already reported this week; not repeating.")
            stale = []
        findings += stale

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
