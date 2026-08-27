-- 244_ref_holidays.sql
-- reference.ref_holidays: the ONE holiday calendar with holiday TYPE.
--
-- Why: before this there was no table that says whether a date is a PH regular holiday,
-- a special (non-working) day, or a special (working) day. analytics.v_calendar_holiday only
-- mirrors whatever someone put on the Google Calendar (8 PH dates in 2026, no type),
-- scorecard.holidays / reference.ref_scrub_holidays are date-only company lists, and
-- reference.fn_approval_deadline (158) explicitly ignores PH holidays. Requested by Jamil
-- 2026-08-27 ("aug 21 ninoy aquino day and its special non working day").
--
-- Grain: one row per (calendar, holiday_date, holiday_type).
--   calendar      'PH'    official Philippine holidays (Malacanang proclamations)
--                 'US'    US federal holidays as observed (5 U.S.C. 6103 / OPM calendar)
--                 'ONTEL' company-observed days, copied one-time from scorecard.holidays
--   holiday_type  'regular' | 'special_non_working' | 'special_working' (PH, DOLE pay rules)
--                 'federal' (US) | 'company' (ONTEL)
--   is_non_working  true = no work expected that day; false only for special_working.
--   proclamation_ref / source  provenance per row so a later year can be added the same way.
--
-- Coverage seeded here: PH 2025 + 2026 (nationwide only; locality-specific special days are
-- deliberately excluded), US 2025 + 2026, ONTEL 2021-12-31..2026-07-03 (31 rows, as-is).
-- Adding a year = one INSERT block with the new proclamation number. Islamic holidays
-- (Eid'l Fitr / Eid'l Adha) are declared by separate proclamations each year; they are
-- NOT in the annual list and must be added when Malacanang issues them.
--
-- Sources (verified 2026-08-27):
--   Proclamation No. 1006, s. 2025 (signed 2025-09-03): 2026 regular/special days
--   Proclamation No. 1189, s. 2026: Eid'l Fitr 2026-03-20 regular holiday
--   Proclamation No. 1264, s. 2026: Eid'l Adha 2026-05-27 regular holiday
--   Proclamation No. 727,  s. 2024 (signed 2024-10-30): 2025 regular/special days
--   Proclamation No. 729,  s. 2024: 2025-07-27 INC anniversary special (non-working) day
--   Proclamation No. 839,  s. 2025: Eid'l Fitr 2025-04-01 regular holiday
--   Proclamation No. 911,  s. 2025: Eid'l Adha 2025-06-06 regular holiday
--   Proclamation No. 878,  s. 2025: 2025-05-12 national/local elections special (non-working) day
--
-- Rollback:
--   DROP TABLE IF EXISTS reference.ref_holidays;
--   DELETE FROM agent.schema_metadata WHERE schema_name='reference' AND table_name='ref_holidays';

BEGIN;

CREATE TABLE IF NOT EXISTS reference.ref_holidays (
    calendar         text        NOT NULL CHECK (calendar IN ('PH', 'US', 'ONTEL')),
    holiday_date     date        NOT NULL,
    holiday_type     text        NOT NULL CHECK (holiday_type IN
                                   ('regular', 'special_non_working', 'special_working', 'federal', 'company')),
    name             text        NOT NULL,
    is_non_working   boolean     NOT NULL,
    proclamation_ref text,
    source           text        NOT NULL,
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (calendar, holiday_date, holiday_type),
    CONSTRAINT ref_holidays_type_matches_calendar CHECK (
        (calendar = 'PH'    AND holiday_type IN ('regular', 'special_non_working', 'special_working')) OR
        (calendar = 'US'    AND holiday_type = 'federal') OR
        (calendar = 'ONTEL' AND holiday_type = 'company')),
    CONSTRAINT ref_holidays_working_flag CHECK (is_non_working = (holiday_type <> 'special_working'))
);
ALTER TABLE reference.ref_holidays ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS ref_holidays_date_idx ON reference.ref_holidays (holiday_date);

COMMENT ON TABLE reference.ref_holidays IS
  'Holiday calendar with type: PH regular / special (non-working) / special (working) per Malacanang proclamation, US federal, and Ontel company-observed days. One row per (calendar, holiday_date, holiday_type).';

-- ---------------------------------------------------------------------------
-- PH 2025  (Proclamation No. 727, s. 2024 unless noted)
-- ---------------------------------------------------------------------------
INSERT INTO reference.ref_holidays (calendar, holiday_date, holiday_type, name, is_non_working, proclamation_ref, source) VALUES
('PH','2025-01-01','regular','New Year''s Day',                       true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-04-01','regular','Eid''l Fitr (Feast of Ramadhan)',       true,'Proclamation No. 839, s. 2025','pco.gov.ph'),
('PH','2025-04-09','regular','Araw ng Kagitingan',                    true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-04-17','regular','Maundy Thursday',                       true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-04-18','regular','Good Friday',                           true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-05-01','regular','Labor Day',                             true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-06-06','regular','Eid''l Adha (Feast of Sacrifice)',      true,'Proclamation No. 911, s. 2025','pco.gov.ph'),
('PH','2025-06-12','regular','Independence Day',                      true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-08-25','regular','National Heroes Day',                   true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-11-30','regular','Bonifacio Day',                         true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-12-25','regular','Christmas Day',                         true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-12-30','regular','Rizal Day',                             true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-01-29','special_non_working','Chinese New Year',          true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-04-19','special_non_working','Black Saturday',            true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-05-12','special_non_working','National and Local Elections', true,'Proclamation No. 878, s. 2025','pco.gov.ph'),
('PH','2025-07-27','special_non_working','Iglesia ni Cristo Founding Anniversary', true,'Proclamation No. 729, s. 2024','pco.gov.ph'),
('PH','2025-08-21','special_non_working','Ninoy Aquino Day',          true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-10-31','special_non_working','All Saints'' Day Eve',      true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-11-01','special_non_working','All Saints'' Day',          true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-12-08','special_non_working','Feast of the Immaculate Conception of Mary', true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-12-24','special_non_working','Christmas Eve',             true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-12-31','special_non_working','Last Day of the Year',      true,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html'),
('PH','2025-02-25','special_working','EDSA People Power Revolution Anniversary', false,'Proclamation No. 727, s. 2024','lawphil.net/executive/proc/proc2024/proc_727_2024.html')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- PH 2026  (Proclamation No. 1006, s. 2025 unless noted)
-- ---------------------------------------------------------------------------
INSERT INTO reference.ref_holidays (calendar, holiday_date, holiday_type, name, is_non_working, proclamation_ref, source) VALUES
('PH','2026-01-01','regular','New Year''s Day',                       true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-03-20','regular','Eid''l Fitr (Feast of Ramadhan)',       true,'Proclamation No. 1189, s. 2026','pco.gov.ph'),
('PH','2026-04-02','regular','Maundy Thursday',                       true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-04-03','regular','Good Friday',                           true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-04-09','regular','Araw ng Kagitingan',                    true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-05-01','regular','Labor Day',                             true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-05-27','regular','Eid''l Adha (Feast of Sacrifice)',      true,'Proclamation No. 1264, s. 2026','officialgazette.gov.ph/2026/05/21/proclamation-no-1264-s-2026'),
('PH','2026-06-12','regular','Independence Day',                      true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-08-31','regular','National Heroes Day',                   true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-11-30','regular','Bonifacio Day',                         true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-12-25','regular','Christmas Day',                         true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-12-30','regular','Rizal Day',                             true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-02-17','special_non_working','Chinese New Year',          true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-04-04','special_non_working','Black Saturday',            true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-08-21','special_non_working','Ninoy Aquino Day',          true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-11-01','special_non_working','All Saints'' Day',          true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-11-02','special_non_working','All Souls'' Day',           true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-12-08','special_non_working','Feast of the Immaculate Conception of Mary', true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-12-24','special_non_working','Christmas Eve',             true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-12-31','special_non_working','Last Day of the Year',      true,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html'),
('PH','2026-02-25','special_working','EDSA People Power Revolution Anniversary', false,'Proclamation No. 1006, s. 2025','lawphil.net/executive/proc/proc2025/proc_1006_2025.html')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- US federal holidays 2025 + 2026 (observed dates per OPM; 2026-07-03 observes Jul 4 Sat)
-- ---------------------------------------------------------------------------
INSERT INTO reference.ref_holidays (calendar, holiday_date, holiday_type, name, is_non_working, proclamation_ref, source, notes) VALUES
('US','2025-01-01','federal','New Year''s Day',                 true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-01-20','federal','Birthday of Martin Luther King, Jr.', true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-02-17','federal','Washington''s Birthday',          true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-05-26','federal','Memorial Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-06-19','federal','Juneteenth National Independence Day', true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-07-04','federal','Independence Day',                true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-09-01','federal','Labor Day',                       true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-10-13','federal','Columbus Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-11-11','federal','Veterans Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-11-27','federal','Thanksgiving Day',                true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2025-12-25','federal','Christmas Day',                   true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-01-01','federal','New Year''s Day',                 true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-01-19','federal','Birthday of Martin Luther King, Jr.', true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-02-16','federal','Washington''s Birthday',          true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-05-25','federal','Memorial Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-06-19','federal','Juneteenth National Independence Day', true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-07-03','federal','Independence Day (observed)',     true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', 'Jul 4 2026 falls on Saturday; observed Friday Jul 3'),
('US','2026-09-07','federal','Labor Day',                       true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-10-12','federal','Columbus Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-11-11','federal','Veterans Day',                    true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-11-26','federal','Thanksgiving Day',                true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL),
('US','2026-12-25','federal','Christmas Day',                   true,'5 U.S.C. 6103','opm.gov/policy-data-oversight/pay-leave/federal-holidays', NULL)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- ONTEL company-observed days: one-time copy of scorecard.holidays (31 rows, 2021-12-31..2026-07-03).
-- Read-only copy; scorecard is not our schema. The original remark ('Company Holiday' /
-- 'US Holiday') is kept in notes.
-- ---------------------------------------------------------------------------
INSERT INTO reference.ref_holidays (calendar, holiday_date, holiday_type, name, is_non_working, proclamation_ref, source, notes)
SELECT 'ONTEL', h.holiday_date, 'company', coalesce(h.remarks, 'Company Holiday'), true, NULL,
       'scorecard.holidays (one-time copy 2026-08-27)', h.remarks
FROM scorecard.holidays h
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- AI semantic layer registration (table-level row, column_name NULL; convention of 206/217/243)
-- ---------------------------------------------------------------------------
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT 'reference', 'ref_holidays', NULL,
  'Holiday calendar with TYPE. calendar = PH (Malacanang proclamations: regular / special_non_working / special_working), US (federal, observed dates), ONTEL (company-observed days copied from scorecard.holidays). One row per (calendar, holiday_date, holiday_type); is_non_working is false only for PH special_working days (e.g. EDSA anniversary).',
  'Use this, not analytics.v_calendar_holiday (Google Calendar mirror, incomplete, no type), to decide whether a PHT work date is a holiday and what kind (DOLE pay rules differ: regular = 200%, special non-working = 130%). Seeded PH 2025-2026, US 2025-2026. Eid''l Fitr / Eid''l Adha are proclaimed separately each year and must be added when issued; a new year = one INSERT block with its proclamation number.',
  'Nationwide PH days only; locality-specific special days excluded. Sources per row in proclamation_ref/source. scorecard.holidays copy is as-is (its remarks were inconsistent: Jul 4 tagged both Company and US Holiday in different years).',
  array['analytics.v_calendar_holiday', 'reference.ref_scrub_holidays', 'scorecard.holidays']::text[]
WHERE NOT EXISTS (
  SELECT 1 FROM agent.schema_metadata m
  WHERE m.schema_name = 'reference' AND m.table_name = 'ref_holidays' AND m.column_name IS NULL);

COMMIT;

NOTIFY pgrst, 'reload schema';
