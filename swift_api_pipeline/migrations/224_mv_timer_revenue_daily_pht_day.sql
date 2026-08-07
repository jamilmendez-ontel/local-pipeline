-- 224: fix analytics.mv_timer_revenue_daily's work_date — it was a MONTH, not a day
--
-- Migration 214 grouped the per-day proration by
-- data_staging.stg_timer_activities_clean.start_date. That column is NOT the
-- work day: it is a MONTH BUCKET. Verified read-only 2026-08-07 on live data:
--
--   start_date    n       min(start_time)      max(start_time)      distinct PHT days
--   2026-06-01    11,524  2026-06-01 05:59Z    2026-07-01 03:56Z    31
--   2026-07-01    11,971  2026-07-01 04:00Z    2026-08-01 03:55Z    32
--   2026-08-01     2,215  2026-08-01 07:08Z    2026-08-07 03:36Z     7
--
-- Every other calendar date carries 1-5 stray rows. So the MV's `work_date`
-- landed ~all revenue on the 1st of each month. The consumers join on
-- (user_email, work_date) against the PHT work day (analytics.v_hr_report_review
-- .work_date), so the DR Monitoring / DR Approval "Attributed $" column and the
-- variance revenue basis would read as empty on every day except the 1st. The
-- 2026-08-03 pre-apply validation missed it: it checked totals and per-group
-- drift, both of which are correct regardless of how the days are labeled, and
-- never checked the date distribution.
--
-- Fix: derive the day from start_time, the true timestamptz instant, in
-- Asia/Manila — the same PHT work day the HR pages key on. UTC would be wrong
-- here: PHT is UTC+8 and night-shift work starting 20:00-23:59 PHT sits on the
-- PREVIOUS UTC day, which is exactly the population the variance pages care
-- about most.
--
-- A materialized view's query cannot be replaced in place, so this drops and
-- recreates. Safe to do: nothing in production consumes this MV (the app-side
-- revenue embed was reverted 2026-08-03 in ontel-people PR #50), and
-- refresh_analytics() does not list it until the same PR that carries this file
-- merges. refresh_one_mv()'s whitelist branch needs no change: the name is
-- unchanged.
--
-- PRE-FLIGHT, read-only against live data 2026-08-07 (proposed definition vs
-- analytics.mv_timer_revenue as the parent):
--   * 134,287 child rows over 916 distinct days, 2023-01-03 .. 2026-08-01
--     (was 123,564 rows over ~44 month-buckets)
--   * 122,310 of 123,226 parent groups matched; proration drift on matched
--     groups $0.12 total, max $0.01 per group (cent rounding only, unchanged
--     from 214's guarantee that window shares sum to the parent amount)
--   * the 916 unmatched parent groups ($53,659.59) are parent/child snapshot
--     skew, not a proration defect: the parent was last refreshed at a
--     different moment than the current stg_timer_activities_clean contents.
--     transform.py refreshes parent then child back-to-back in one cycle,
--     which is what keeps them consistent in normal operation.

DROP MATERIALIZED VIEW IF EXISTS analytics.mv_timer_revenue_daily;

CREATE MATERIALIZED VIEW analytics.mv_timer_revenue_daily AS
WITH day_minutes AS (
    SELECT t.asset_did, t.task_clean, t.user_email,
           -- PHT work day from the true instant. NOT t.start_date (month bucket)
           -- and NOT a UTC cast (night shifts would land a day early).
           (t.start_time AT TIME ZONE 'Asia/Manila')::date AS work_date,
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

GRANT SELECT ON analytics.mv_timer_revenue_daily TO service_role;
