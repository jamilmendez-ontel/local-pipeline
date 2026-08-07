-- 228: mv_timer_revenue_daily must bucket by ET day, not PHT day
--
-- Migration 225 fixed the real bug in 214 (work_date was built from
-- stg_timer_activities_clean.start_date, which is a MONTH bucket) but picked the
-- wrong timezone for the replacement. It used Asia/Manila on the reasoning that
-- work_date is "the PHT work day". That reasoning was wrong.
--
-- The canonical attribution of timer entries to a daily report in this warehouse
-- is the EASTERN day. analytics.mv_timer_day_rollup, which is where
-- v_hr_report_review.timed_hours / open_timer_count / timer_entry_count come
-- from, buckets on:
--     (start_time AT TIME ZONE 'America/New_York')::date AS work_day
-- and the app agrees: getDayActivitiesBatch fetches a report's timer log with
-- etDayRangeUtc(workDate). Before this migration mv_timer_revenue_daily was the
-- ONLY timer object in analytics using Asia/Manila.
--
-- SYMPTOM this fixes. PHT is UTC+8 and ET is UTC-4/-5, so a PHT day covers the
-- back half of one ET day and the front half of the next. Revenue therefore
-- landed roughly one day LATER than the report it belonged to, and a report row
-- with no timer entries at all could still show money. Observed on live data
-- 2026-08-07:
--
--   member          work_date    timed_hours   PHT (wrong)   ET (correct)
--   Jason Nobleza   2026-08-06      5.2         $272.84        $100.00
--   Jason Nobleza   2026-08-07      (none)      $100.00        (none)
--   Imee Pammit     2026-08-06      7.6         $526.33        $488.56
--   Imee Pammit     2026-08-07      (none)      $319.52        (none)
--
-- Both 08-07 rows were "submitted, no entries, 0.0 hrs" and still displayed a
-- revenue figure, because their PHT-08-07 timers are ET-08-06 work. Under ET the
-- money moves back onto the day whose report actually carries the hours.
--
-- PRE-FLIGHT, read-only on live data 2026-08-07 (proposed definition vs
-- analytics.mv_timer_revenue as parent):
--   * 130,148 child rows over 908 distinct days, 2023-01-03 .. 2026-08-06
--     (max day is 08-06 because ET 08-07 had barely started)
--   * child total $7,627,972.91 vs parent $7,627,973.06: drift $0.15, cent
--     rounding only, the same guarantee 214 and 225 carried
--   * every sampled member-day with revenue now also has timer hours on that
--     same report row
--
-- Safe to drop and recreate: refresh_one_mv's whitelist keys on the name, which
-- does not change, and this MV is not yet consumed by anything in production
-- (the app-side revenue embed is still on a branch).

-- analytics.v_timer_revenue_member_day (migration 226) reads this MV, so it has
-- to come down with it and be re-emitted below, unchanged, in the same
-- transaction. Dropping it CASCADE-style without re-creating would silently take
-- the variance dashboard's revenue read offline.
DROP VIEW IF EXISTS analytics.v_timer_revenue_member_day;
DROP MATERIALIZED VIEW IF EXISTS analytics.mv_timer_revenue_daily;

CREATE MATERIALIZED VIEW analytics.mv_timer_revenue_daily AS
WITH day_minutes AS (
    SELECT t.asset_did, t.task_clean, t.user_email,
           -- EASTERN day, mirroring analytics.mv_timer_day_rollup.work_day so
           -- revenue and timed_hours describe the same day for the same report.
           -- NOT Asia/Manila (225's mistake) and NOT t.start_date (214's, a
           -- month bucket).
           (t.start_time AT TIME ZONE 'America/New_York')::date AS work_date,
           sum(t.duration_min) AS day_minutes
    FROM data_staging.stg_timer_activities_clean t
    WHERE t.asset_did IS NOT NULL
      AND t.task_clean IS NOT NULL
      AND t.duration_min > 0
      AND t.start_time IS NOT NULL
    GROUP BY 1, 2, 3, 4
)
SELECT
    mv.asset_did,
    mv.task_clean,
    mv.user_email,
    d.work_date,
    mv.user_name,
    mv.market_bucket,
    mv.pricing_status,
    round(d.day_minutes, 2) AS day_minutes,
    round(
        (mv.amount_usd * d.day_minutes
         / sum(d.day_minutes) OVER (PARTITION BY mv.asset_did, mv.task_clean, mv.user_email)
        )::numeric, 2) AS amount_usd
FROM analytics.mv_timer_revenue mv
JOIN day_minutes d
  ON d.asset_did = mv.asset_did
 AND d.task_clean = mv.task_clean
 AND d.user_email = mv.user_email;

-- Required for REFRESH ... CONCURRENTLY (parent grain + work_date)
CREATE UNIQUE INDEX mv_timer_revenue_daily_pk
    ON analytics.mv_timer_revenue_daily (asset_did, task_clean, user_email, work_date);
CREATE INDEX mv_timer_revenue_daily_email_date_idx
    ON analytics.mv_timer_revenue_daily (user_email, work_date);
CREATE INDEX mv_timer_revenue_daily_date_idx
    ON analytics.mv_timer_revenue_daily (work_date);
-- Case-proof correlation key for hr_review_page/hr_review_count's revenue
-- range filter (migration 227); recreated because the MV was dropped.
CREATE INDEX mv_timer_revenue_daily_lower_email_date_idx
    ON analytics.mv_timer_revenue_daily (lower(user_email), work_date);

GRANT SELECT ON analytics.mv_timer_revenue_daily TO service_role;

-- Re-emit migration 226's rollup verbatim (it is unchanged; it only had to be
-- dropped to let the MV underneath it be replaced).
CREATE OR REPLACE VIEW analytics.v_timer_revenue_member_day AS
SELECT
    lower(d.user_email) AS user_email,
    d.work_date,
    sum(d.amount_usd) FILTER (WHERE d.pricing_status = 'priced') AS attributed_usd,
    sum(d.day_minutes) FILTER (WHERE d.pricing_status = 'priced') AS priced_minutes,
    sum(d.day_minutes) AS total_minutes
FROM analytics.mv_timer_revenue_daily d
WHERE d.user_email IS NOT NULL
GROUP BY lower(d.user_email), d.work_date;

COMMENT ON VIEW analytics.v_timer_revenue_member_day IS
'Member-day rollup of analytics.mv_timer_revenue_daily for the ontel-people variance dashboard and the DR Monitoring revenue column. attributed_usd sums only priced rows; priced_minutes/total_minutes are the coverage ratio. Emails are lowercased so app-side joins need no per-row normalization. Migration 226, re-emitted by 228.';

GRANT SELECT ON analytics.v_timer_revenue_member_day TO service_role;
