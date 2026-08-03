-- 214: analytics.mv_timer_revenue_daily — per-day proration of mv_timer_revenue
-- Serving layer for the ontel-people revenue embed (variance KPI, DR Monitoring /
-- DR Approval day columns; spec: ontel-people docs/superpowers/specs/
-- 2026-08-03-timer-revenue-embed-design.md). mv_timer_revenue stamps a LIFETIME
-- amount per (asset, task, member) with only first/last work dates; last_work_date
-- = max(end_date) is unreliable (45% of groups land a different month than the
-- work, avg 68 days late — timer-runaway residue), and the HR pages join on
-- (email, work_date). This MV splits each group's amount across the member's
-- actual timer days by minute share: for the ~99% single-day groups a pass-
-- through, for the 1,226 multi-day groups a true split. amount_usd is NULL for
-- unpriced rows (pricing_status explains why), 0 for lr_unapproved_zero.
-- Depends on mv_timer_revenue, so refresh_analytics() MUST refresh it AFTER the
-- parent (transform.py ships the ordered list in the same PR). Validated
-- read-only 2026-08-03 pre-apply: 123,568 daily rows / 122,310 parent groups,
-- 0 parent groups missing, total drift 0.0000 (window-share proration sums to
-- the parent amount by construction; only display rounding differs).
-- Applied to prod 2026-08-03; verified: 123,568 rows / $7,558,012.04 total,
-- total drift vs parent $0.02, max per-group drift $0.01 (cent rounding only),
-- refresh_one_mv('mv_timer_revenue_daily') CONCURRENTLY in 3,740ms.

CREATE MATERIALIZED VIEW analytics.mv_timer_revenue_daily AS
WITH day_minutes AS (
    SELECT t.asset_did, t.task_clean, t.user_email,
           t.start_date AS work_date,
           sum(t.duration_min) AS day_minutes
    FROM data_staging.stg_timer_activities_clean t
    WHERE t.asset_did IS NOT NULL
      AND t.task_clean IS NOT NULL
      AND t.duration_min > 0
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

-- Required for REFRESH ... CONCURRENTLY (parent grain + work_date; parent pk
-- already verified NULL-free on these columns)
CREATE UNIQUE INDEX mv_timer_revenue_daily_pk
    ON analytics.mv_timer_revenue_daily (asset_did, task_clean, user_email, work_date);
CREATE INDEX mv_timer_revenue_daily_email_date_idx
    ON analytics.mv_timer_revenue_daily (user_email, work_date);
CREATE INDEX mv_timer_revenue_daily_date_idx
    ON analytics.mv_timer_revenue_daily (work_date);

GRANT SELECT ON analytics.mv_timer_revenue_daily TO service_role;

-- Extend the refresh whitelist (full re-emit of the current live def from
-- migration 211 + one new branch before ELSE)
CREATE OR REPLACE FUNCTION analytics.refresh_one_mv(p_view_name text)
 RETURNS TABLE(view_name text, refresh_time_ms bigint)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET statement_timeout TO '300s'
AS $function$
DECLARE
    start_ts TIMESTAMPTZ;
    end_ts TIMESTAMPTZ;
BEGIN
    start_ts := clock_timestamp();
    IF p_view_name = 'mv_project_summary' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary;
    ELSIF p_view_name = 'mv_technician_stats' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats;
    ELSIF p_view_name = 'mv_daily_completion' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion;
    ELSIF p_view_name = 'mv_project_summary_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary_gc;
    ELSIF p_view_name = 'mv_technician_stats_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats_gc;
    ELSIF p_view_name = 'mv_daily_completion_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion_gc;
    ELSIF p_view_name = 'mv_quote_invoice_options' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_invoice_options;
    ELSIF p_view_name = 'mv_quote_review' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_review;
    ELSIF p_view_name = 'mv_quote_source_invoice_lines' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_source_invoice_lines;
    ELSIF p_view_name = 'mv_timer_revenue' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue;
    ELSIF p_view_name = 'mv_timer_revenue_daily' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue_daily;
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;
    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$function$;
