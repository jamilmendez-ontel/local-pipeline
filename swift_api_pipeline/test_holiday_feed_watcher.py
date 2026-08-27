"""Unit tests for holiday_feed_watcher's pure evaluators and the feed walk
(mocked session; no network, no DB).
Run: cd swift_api_pipeline && python -m pytest test_holiday_feed_watcher.py -v
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from holiday_feed_watcher import (
    parse_feed_page, parse_gazette_feed, classify_subject, evaluate_gazette,
    evaluate_nager, evaluate_staleness, build_email_body, fetch_gazette,
    coverage_gap_finding, item_key,
)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Official Gazette</title>
<item><title>Proclamation No. 1409 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/08/26/proclamation-no-1409-s-2026/</link>
<pubDate>Wed, 26 Aug 2026 12:30:31 +0000</pubDate>
<description><![CDATA[MALACAÑAN PALACE MANILA BY THE PRESIDENT OF THE PHILIPPINES PROCLAMATION NO. 1409 DECLARING MONDAY, 7 SEPTEMBER 2026, A SPECIAL (NON-WORKING) DAY IN THE CITY OF STO. TOMAS, PROVINCE OF BATANGAS]]></description></item>
<item><title>Executive Order No. 118 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/08/25/eo-118/</link>
<pubDate>Tue, 25 Aug 2026 09:00:00 +0000</pubDate>
<description><![CDATA[IMPOSITION OF A MANDATED PRICE CEILING ON IMPORTED RICE]]></description></item>
<item><title>Proclamation No. 1264 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/05/21/proclamation-no-1264-s-2026/</link>
<pubDate>Thu, 21 May 2026 08:00:00 +0000</pubDate>
<description><![CDATA[DECLARING WEDNESDAY, 27 MAY 2026, A REGULAR HOLIDAY THROUGHOUT THE COUNTRY, IN OBSERVANCE OF THE EID'L ADHA (FEAST OF SACRIFICE)&#160;]]></description></item>
</channel></rss>"""

DB = {
    "2026-08-21": {"holiday_type": "special_non_working", "name": "Ninoy Aquino Day",
                   "proclamation_ref": "Proclamation No. 1006, s. 2025"},
    "2026-05-27": {"holiday_type": "regular", "name": "Eid'l Adha (Feast of Sacrifice)",
                   "proclamation_ref": "Proclamation No. 1264, s. 2026"},
    "2026-12-31": {"holiday_type": "special_non_working", "name": "Last Day of the Year",
                   "proclamation_ref": "Proclamation No. 1006, s. 2025"},
}
TODAY = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


def item(no, subject, year=2026, published=NOW):
    return {"proc_no": no, "year": year, "title": f"Proclamation No. {no} s. {year}",
            "link": f"https://www.officialgazette.gov.ph/{year}/x/proclamation-no-{no}-s-{year}/",
            "subject": subject, "published": published}


# --- parsing -----------------------------------------------------------------

def test_parse_feed_keeps_only_proclamations_counts_raw_and_cleans_subject():
    items, raw = parse_feed_page(FEED)
    assert [i["proc_no"] for i in items] == [1409, 1264]
    assert len(raw) == 3                                   # EO counted as a raw post
    assert raw[0] == datetime(2026, 8, 26, 12, 30, 31, tzinfo=timezone.utc)
    assert items[1]["subject"].endswith("(FEAST OF SACRIFICE)")   # &#160; stripped
    assert items[0]["link"].endswith("proclamation-no-1409-s-2026/")
    assert parse_gazette_feed("﻿ \n" + FEED)[0]["proc_no"] == 1409   # BOM / leading ws


@pytest.mark.parametrize("body", [
    "<!DOCTYPE html><html><body><h1>Just a moment...</h1></body></html>",
    "<html><body><h1>403</h1></body></html>",
    "<?xml version='1.0'?><urlset><url/></urlset>",
    "<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><rss><channel><item><title>&a;</title></item></channel></rss>",
])
def test_parse_feed_rejects_non_rss_bodies(body):
    with pytest.raises(ValueError):
        parse_feed_page(body)


# --- classification ------------------------------------------------------------

def test_classify_local_special_day():
    c = classify_subject("DECLARING MONDAY, 7 SEPTEMBER 2026, A SPECIAL (NON-WORKING) DAY "
                         "IN THE CITY OF STO. TOMAS, PROVINCE OF BATANGAS")
    assert c["kind"] == "holiday_declaration"
    assert c["scope"] == "local"
    assert c["holiday_type"] == "special_non_working"
    assert c["dates"] == ["2026-09-07"]


def test_classify_nationwide_regular_with_name():
    c = classify_subject("DECLARING WEDNESDAY, 27 MAY 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                         "COUNTRY, IN OBSERVANCE OF THE EID'L ADHA (FEAST OF SACRIFICE)")
    assert c["scope"] == "nationwide"
    assert c["holiday_type"] == "regular"
    assert c["dates"] == ["2026-05-27"]
    assert c["name"] == "Eid'l Adha"


def test_classify_annual_list_amendment_multi_date_and_other():
    a = classify_subject("DECLARING THE REGULAR HOLIDAYS AND SPECIAL (NON-WORKING) DAYS FOR THE YEAR 2027")
    assert a["kind"] == "annual_list" and a["annual_year"] == 2027
    m = classify_subject("AMENDING PROCLAMATION NO. 1006 (S. 2025) BY DECLARING MONDAY, "
                         "2 NOVEMBER 2026 A REGULAR HOLIDAY THROUGHOUT THE COUNTRY")
    assert m["amends"] == 1006 and m["holiday_type"] == "regular" and m["dates"] == ["2026-11-02"]
    d = classify_subject("DECLARING THURSDAY, 24 DECEMBER 2026 AND THURSDAY, 31 DECEMBER 2026 AS "
                         "ADDITIONAL SPECIAL (NON-WORKING) DAYS THROUGHOUT THE COUNTRY")
    assert d["dates"] == ["2026-12-24", "2026-12-31"]
    o = classify_subject("CALLING THE CONGRESS OF THE PHILIPPINES TO A SPECIAL SESSION")
    assert o["kind"] == "other"


def test_classify_mixed_types_yields_no_type():
    # amendment quoting the old type, and a two-date mixed declaration: never SQL
    c = classify_subject("AMENDING PROCLAMATION NO. 1006 BY DECLARING TUESDAY, 3 NOVEMBER 2026, "
                         "PREVIOUSLY A REGULAR HOLIDAY, A SPECIAL (NON-WORKING) DAY THROUGHOUT THE COUNTRY")
    assert c["kind"] == "holiday_declaration" and c["holiday_type"] is None
    c = classify_subject("DECLARING 2 NOVEMBER 2026 A REGULAR HOLIDAY AND 3 NOVEMBER 2026 A "
                         "SPECIAL (NON-WORKING) DAY THROUGHOUT THE COUNTRY")
    assert c["holiday_type"] is None and c["dates"] == ["2026-11-02", "2026-11-03"]
    # a special (working) day is its own type, not "regular"
    c = classify_subject("DECLARING WEDNESDAY, 25 FEBRUARY 2026, A SPECIAL (WORKING) DAY THROUGHOUT THE COUNTRY")
    assert c["holiday_type"] == "special_working"


# --- gazette evaluation --------------------------------------------------------

def test_evaluate_gazette_new_date_proposes_insert_and_ignores_local():
    items = [
        item(1500, "DECLARING MONDAY, 2 NOVEMBER 2026, A SPECIAL (NON-WORKING) DAY THROUGHOUT "
                   "THE COUNTRY, IN OBSERVANCE OF ALL SOULS' DAY"),
        item(1499, "DECLARING FRIDAY, 28 AUGUST 2026, A SPECIAL (NON-WORKING) DAY IN THE "
                   "MUNICIPALITY OF KUMALARANG, PROVINCE OF ZAMBOANGA DEL SUR"),
    ]
    findings, scanned = evaluate_gazette(items, DB, set(), TODAY)
    assert scanned == ["2026:1500", "2026:1499"]           # local items are scanned too
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "new_nationwide" and f["date"] == "2026-11-02"
    assert f["holiday_type"] == "special_non_working"
    assert "INSERT INTO reference.ref_holidays" in f["sql"]
    assert "'2026-11-02', 'special_non_working', 'All Souls'' Day', true" in f["sql"]
    assert "'Proclamation No. 1500, s. 2026'" in f["sql"]


def test_evaluate_gazette_type_change_proposes_update():
    items = [item(1501, "DECLARING FRIDAY, 21 AUGUST 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                        "COUNTRY, IN OBSERVANCE OF NINOY AQUINO DAY")]
    findings, _ = evaluate_gazette(items, DB, set(), TODAY)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "type_change"
    assert f["previous_type"] == "special_non_working" and f["holiday_type"] == "regular"
    assert f["sql"].startswith("UPDATE reference.ref_holidays SET holiday_type = 'regular', is_non_working = true")
    assert "amended_by = 'Proclamation No. 1501, s. 2026'" in f["sql"]
    assert "holiday_date = '2026-08-21'" in f["sql"]


def test_evaluate_gazette_known_same_type_silent_and_seen_keys_filter():
    known = item(1264, "DECLARING WEDNESDAY, 27 MAY 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                       "COUNTRY, IN OBSERVANCE OF THE EID'L ADHA (FEAST OF SACRIFICE)")
    late = item(1300, "DECLARING MONDAY, 2 NOVEMBER 2026, A REGULAR HOLIDAY THROUGHOUT THE COUNTRY")
    # 1300 already scanned by an earlier run -> silent even though the date is unknown
    findings, scanned = evaluate_gazette([known, late], DB, {"2026:1300"}, TODAY)
    assert findings == [] and scanned == ["2026:1264", "2026:1300"]
    # a late-posted lower number that no run has scanned fires (numeric watermark would have lost it)
    findings, _ = evaluate_gazette([known, late], DB, {"2026:1264", "2026:1409"}, TODAY)
    assert [f["kind"] for f in findings] == ["new_nationwide"]
    assert item_key(late) == "2026:1300"


def test_evaluate_gazette_annual_list_unparseable_and_date_sanity():
    items = [item(1600, "DECLARING THE REGULAR HOLIDAYS AND SPECIAL (NON-WORKING) DAYS FOR THE YEAR 2027"),
             item(1601, "DECLARING A SPECIAL (NON-WORKING) DAY FOR THE VICTORY PARADE"),
             item(1602, "AMENDING PROCLAMATION NO. 1006 BY DECLARING 3 NOVEMBER 2026, PREVIOUSLY A "
                        "REGULAR HOLIDAY, A SPECIAL (NON-WORKING) DAY THROUGHOUT THE COUNTRY"),
             item(1603, "DECLARING MONDAY, 1 JANUARY 1999, A REGULAR HOLIDAY THROUGHOUT THE COUNTRY")]
    findings, _ = evaluate_gazette(items, DB, set(), TODAY)
    assert [f["kind"] for f in findings] == ["annual_list", "review", "review", "review"]
    assert findings[0]["year"] == 2027
    assert all("sql" not in f for f in findings)


# --- feed walk (mocked session) ------------------------------------------------

def rss(*entries):
    """entries: (title, pubdate iso or None). Builds a minimal RSS page."""
    body = ""
    for title, pub in entries:
        pd = "" if pub is None else f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        body += (f"<item><title>{title}</title><link>https://x/{title.replace(' ', '-')}</link>{pd}"
                 f"<description>DECLARING MONDAY, 7 SEPTEMBER 2026, A SPECIAL (NON-WORKING) DAY IN THE "
                 f"CITY OF X, PROVINCE OF Y</description></item>")
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{body}</channel></rss>"


class FakeResp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        n = 1 if "paged=" not in url else int(url.rsplit("=", 1)[1])
        return self.pages.get(n, FakeResp(404))


def d(days_ago):
    return NOW - timedelta(days=days_ago)


def test_fetch_gazette_walks_through_proclamation_free_pages_to_cutoff():
    pages = {
        1: FakeResp(200, rss(("Proclamation No. 1412 s. 2026", d(1)))),
        2: FakeResp(200, rss(("Memorandum Circular No. 117", d(3)), ("Executive Order No. 118", d(4)))),
        3: FakeResp(200, rss(("Proclamation No. 1411 s. 2026", d(6)), ("Proclamation No. 1410 s. 2026", d(7)))),
        4: FakeResp(200, rss(("Proclamation No. 1409 s. 2026", d(20)), ("Proclamation No. 1408 s. 2026", d(25)))),
        5: FakeResp(200, rss(("Proclamation No. 1300 s. 2026", d(60)))),
    }
    items, pages_read, reached, oldest = fetch_gazette(FakeSession(pages), cutoff=d(21), max_pages=30)
    # page 2 (no proclamations) must not end the walk; page 4 still has a post newer than
    # the cutoff (d(20) > d(21)) so the walk continues; page 5 is entirely older -> reached.
    assert [i["proc_no"] for i in items] == [1412, 1411, 1410, 1409, 1408, 1300]
    assert pages_read == 5 and reached is True and oldest == d(60)


def test_fetch_gazette_stops_on_page_entirely_older_than_cutoff():
    pages = {
        1: FakeResp(200, rss(("Proclamation No. 1412 s. 2026", d(1)), ("Proclamation No. 1411 s. 2026", d(2)))),
        2: FakeResp(200, rss(("Proclamation No. 1410 s. 2026", d(30)), ("Proclamation No. 1409 s. 2026", d(31)))),
        3: FakeResp(200, rss(("Proclamation No. 1300 s. 2026", d(60)))),
    }
    items, pages_read, reached, oldest = fetch_gazette(FakeSession(pages), cutoff=d(21), max_pages=30)
    assert [i["proc_no"] for i in items] == [1412, 1411, 1410, 1409]
    assert pages_read == 2 and reached is True and oldest == d(31)


def test_fetch_gazette_max_pages_exhausted_is_not_reached():
    pages = {n: FakeResp(200, rss((f"Proclamation No. {1500 - n} s. 2026", d(n)))) for n in range(1, 10)}
    items, pages_read, reached, oldest = fetch_gazette(FakeSession(pages), cutoff=d(21), max_pages=3)
    assert pages_read == 3 and reached is False and oldest == d(3)
    assert [i["proc_no"] for i in items] == [1499, 1498, 1497]
    gap = coverage_gap_finding(pages_read, len(items), d(21))
    assert gap["kind"] == "review" and "NOT advanced" in gap["note"]


def test_fetch_gazette_true_end_of_feed_is_reached():
    pages = {1: FakeResp(200, rss(("Proclamation No. 1412 s. 2026", d(1))))}   # page 2 -> 404
    items, pages_read, reached, _ = fetch_gazette(FakeSession(pages), cutoff=d(21), max_pages=30)
    assert len(items) == 1 and pages_read == 1 and reached is True
    empty = {1: FakeResp(200, rss(("Proclamation No. 1412 s. 2026", d(1)))), 2: FakeResp(200, rss())}
    _, pages_read, reached, _ = fetch_gazette(FakeSession(empty), cutoff=d(21), max_pages=30)
    assert pages_read == 2 and reached is True


def test_fetch_gazette_html_body_raises():
    pages = {1: FakeResp(200, "<html><body>maintenance</body></html>")}
    with pytest.raises(ValueError):
        fetch_gazette(FakeSession(pages), cutoff=d(21), max_pages=30)


# --- nager / staleness / email -------------------------------------------------

def test_evaluate_nager_only_future_unknown_dates_in_seeded_years():
    nager = [{"date": "2026-08-21", "name": "Ninoy Aquino Day"},      # known
             {"date": "2026-08-26", "name": "Maulid un-Nabi"},        # past
             {"date": "2026-11-30", "name": "Bonifacio Day"},         # future, unknown here
             {"date": "2026-11-30", "name": "Bonifacio Day"},         # duplicate
             {"date": "2027-06-12", "name": "Independence Day"},      # unseeded year: ignored
             "garbage", {"date": 20261225}]                           # hostile shapes
    findings = evaluate_nager(nager, DB, TODAY)
    assert [f["date"] for f in findings] == ["2026-11-30"]
    assert findings[0]["kind"] == "nager_only" and "sql" not in findings[0]
    assert evaluate_nager(nager, DB, TODAY, already_reported={"2026-11-30"}) == []
    assert evaluate_nager({"error": "x"}, DB, TODAY) == []              # dict body, not a list


def test_evaluate_staleness():
    assert evaluate_staleness(DB, TODAY) == []                       # 2026-12-31 is 126 days out
    assert evaluate_staleness(DB, date(2026, 9, 10))[0]["kind"] == "stale"
    assert evaluate_staleness({}, TODAY)[0]["kind"] == "stale"


def test_email_body_lists_sql_and_footer():
    items = [item(1501, "DECLARING FRIDAY, 21 AUGUST 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                        "COUNTRY, IN OBSERVANCE OF NINOY AQUINO DAY")]
    findings, _ = evaluate_gazette(items, DB, set(), TODAY)
    findings += evaluate_staleness(DB, date(2026, 9, 10))
    findings.append(coverage_gap_finding(30, 300, d(21)))
    body = build_email_body(findings, NOW, {"pages": 30, "items": 300, "new_items": 12,
                                            "cutoff": d(21).date(), "nager_years": "2026+2027",
                                            "nager_dates": 21})
    assert body.startswith("Holiday feed watch: 3 finding(s)  (checked 2026-08-27 06:00 ET)")
    assert "TYPE CHANGE - Proclamation No. 1501, s. 2026" in body
    assert "special (non-working) day -> regular holiday" in body
    assert "UPDATE reference.ref_holidays" in body
    assert "2. STALE" in body
    assert "3. REVIEW - (coverage gap)" in body
    assert "300 proclamation(s) back to 2026-08-06, 12 not seen before" in body
    assert body.endswith("(holiday_feed_watcher.py; silent when nothing new)")
