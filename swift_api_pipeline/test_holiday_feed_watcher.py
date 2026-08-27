"""Unit tests for holiday_feed_watcher's pure evaluators (no network, no DB).
Run: cd swift_api_pipeline && python -m pytest test_holiday_feed_watcher.py -v
"""

from datetime import date, datetime, timezone

from holiday_feed_watcher import (
    parse_gazette_feed, classify_subject, evaluate_gazette, evaluate_nager,
    evaluate_staleness, build_email_body,
)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Official Gazette</title>
<item><title>Proclamation No. 1409 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/08/26/proclamation-no-1409-s-2026/</link>
<pubDate>Wed, 26 Aug 2026 12:30:31 +0000</pubDate>
<description><![CDATA[MALACAÑAN PALACE MANILA BY THE PRESIDENT OF THE PHILIPPINES PROCLAMATION NO. 1409 DECLARING MONDAY, 7 SEPTEMBER 2026, A SPECIAL (NON-WORKING) DAY IN THE CITY OF STO. TOMAS, PROVINCE OF BATANGAS]]></description></item>
<item><title>Executive Order No. 118 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/08/25/eo-118/</link>
<description><![CDATA[IMPOSITION OF A MANDATED PRICE CEILING ON IMPORTED RICE]]></description></item>
<item><title>Proclamation No. 1264 s. 2026</title>
<link>https://www.officialgazette.gov.ph/2026/05/21/proclamation-no-1264-s-2026/</link>
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


def item(no, subject, year=2026):
    return {"proc_no": no, "year": year, "title": f"Proclamation No. {no} s. {year}",
            "link": f"https://www.officialgazette.gov.ph/{year}/x/proclamation-no-{no}-s-{year}/",
            "subject": subject, "published": ""}


def test_parse_feed_keeps_only_proclamations_and_cleans_subject():
    items = parse_gazette_feed(FEED)
    assert [i["proc_no"] for i in items] == [1409, 1264]
    assert items[0]["year"] == 2026
    assert items[1]["subject"].endswith("(FEAST OF SACRIFICE)")   # &#160; stripped
    assert items[0]["link"].endswith("proclamation-no-1409-s-2026/")


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


def test_evaluate_gazette_new_date_proposes_insert_and_ignores_local():
    items = [
        item(1500, "DECLARING MONDAY, 2 NOVEMBER 2026, A SPECIAL (NON-WORKING) DAY THROUGHOUT "
                   "THE COUNTRY, IN OBSERVANCE OF ALL SOULS' DAY"),
        item(1499, "DECLARING FRIDAY, 28 AUGUST 2026, A SPECIAL (NON-WORKING) DAY IN THE "
                   "MUNICIPALITY OF KUMALARANG, PROVINCE OF ZAMBOANGA DEL SUR"),
    ]
    findings, wm = evaluate_gazette(items, DB, (2026, 1409))
    assert wm == (2026, 1500)
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
    findings, _ = evaluate_gazette(items, DB, (2026, 1409))
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "type_change"
    assert f["previous_type"] == "special_non_working" and f["holiday_type"] == "regular"
    assert f["sql"].startswith("UPDATE reference.ref_holidays SET holiday_type = 'regular', is_non_working = true")
    assert "amended_by = 'Proclamation No. 1501, s. 2026'" in f["sql"]
    assert "holiday_date = '2026-08-21'" in f["sql"]


def test_evaluate_gazette_known_same_type_is_silent_and_watermark_filters():
    known = item(1264, "DECLARING WEDNESDAY, 27 MAY 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                       "COUNTRY, IN OBSERVANCE OF THE EID'L ADHA (FEAST OF SACRIFICE)")
    old = item(1300, "DECLARING MONDAY, 2 NOVEMBER 2026, A REGULAR HOLIDAY THROUGHOUT THE COUNTRY")
    # known + same type -> nothing; 1300 is below the watermark -> skipped even though unknown
    findings, wm = evaluate_gazette([known, old], DB, (2026, 1400))
    assert findings == []
    assert wm == (2026, 1400)
    # with no watermark everything is new: the known one still stays silent, 1300 fires
    findings, wm = evaluate_gazette([known, old], DB, None)
    assert [f["kind"] for f in findings] == ["new_nationwide"]
    assert wm == (2026, 1300)


def test_evaluate_gazette_annual_list_and_unparseable():
    items = [item(1600, "DECLARING THE REGULAR HOLIDAYS AND SPECIAL (NON-WORKING) DAYS FOR THE YEAR 2027"),
             item(1601, "DECLARING A SPECIAL (NON-WORKING) DAY FOR THE VICTORY PARADE")]
    findings, _ = evaluate_gazette(items, DB, (2026, 1409))
    assert [f["kind"] for f in findings] == ["annual_list", "review"]
    assert findings[0]["year"] == 2027


def test_watermark_compares_year_first():
    items = [item(12, "DECLARING MONDAY, 4 JANUARY 2027, A REGULAR HOLIDAY THROUGHOUT THE COUNTRY", year=2027)]
    findings, wm = evaluate_gazette(items, DB, (2026, 1409))   # 12 < 1409 but 2027 > 2026
    assert wm == (2027, 12) and len(findings) == 1


def test_evaluate_nager_only_future_unknown_dates():
    today = date(2026, 8, 27)
    nager = [{"date": "2026-08-21", "name": "Ninoy Aquino Day"},      # known
             {"date": "2026-08-26", "name": "Maulid un-Nabi"},        # past
             {"date": "2026-11-30", "name": "Bonifacio Day"},         # future, unknown here
             {"date": "2026-11-30", "name": "Bonifacio Day"},         # duplicate
             {"date": "2027-06-12", "name": "Independence Day"}]      # unseeded year: ignored
    findings = evaluate_nager(nager, DB, today)
    assert [f["date"] for f in findings] == ["2026-11-30"]
    assert findings[0]["kind"] == "nager_only" and "sql" not in findings[0]
    assert evaluate_nager(nager, DB, today, already_reported={"2026-11-30"}) == []


def test_evaluate_staleness():
    today = date(2026, 8, 27)
    assert evaluate_staleness(DB, today) == []                      # 2026-12-31 is 126 days out
    assert evaluate_staleness(DB, date(2026, 9, 10))[0]["kind"] == "stale"
    assert evaluate_staleness({}, today)[0]["kind"] == "stale"


def test_email_body_lists_sql_and_footer():
    items = [item(1501, "DECLARING FRIDAY, 21 AUGUST 2026, A REGULAR HOLIDAY THROUGHOUT THE "
                        "COUNTRY, IN OBSERVANCE OF NINOY AQUINO DAY")]
    findings, _ = evaluate_gazette(items, DB, (2026, 1409))
    findings += evaluate_staleness(DB, date(2026, 9, 10))
    body = build_email_body(findings, datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
                            {"pages": 2, "items": 15, "watermark_before": "1409 s.2026",
                             "watermark_after": "1501 s.2026", "nager_years": "2026+2027",
                             "nager_dates": 21})
    assert body.startswith("Holiday feed watch: 2 finding(s)  (checked 2026-08-27 06:00 ET)")
    assert "TYPE CHANGE - Proclamation No. 1501, s. 2026" in body
    assert "special (non-working) day -> regular holiday" in body
    assert "UPDATE reference.ref_holidays" in body
    assert "2. STALE" in body
    assert "watermark 1409 s.2026 -> 1501 s.2026" in body
    assert body.endswith("(holiday_feed_watcher.py; silent when nothing new)")
