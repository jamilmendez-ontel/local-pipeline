-- 211: analytics.mv_timer_revenue — timer→revenue phase 2 (Amount attribution)
-- Ports the RevMetrics workbook col-69 "Amount" logic to the DB
-- (docs/specs/timer-revenue-market-crosswalk.md; Excel semantics in
-- reference/revenue-metrics/README.md):
--   revenue = rate(market_bucket, task) × tech's share of cleaned minutes on the
--   (asset, task), so each (asset, task) completion pays out exactly once, split
--   across techs. Final COP / Live Review bundling: LR approval = the asset's
--   'Live Review Complete' asset-task status = 'approved' (DB equivalent of the
--   workbook's Snapshot_LR tab, verified 2026-08-02: 17,534 approved assets).
--   LR not approved → Final COP row absorbs FCOP+LR rates, LR row pays 0.
-- Materialized (not a plain view): the underlying query runs ~9s (market_signature()
-- per asset) — the exact 8s-statement-timeout failure mode from the DRMC brownout.
-- Refreshed post-pipeline via analytics.refresh_one_mv, wired into
-- refresh_analytics() in transform.py.
-- Grain: (asset_did, task_clean, user_email). Admin/no-asset/no-task_clean timer
-- rows are intentionally excluded (never priceable). pricing_status makes the
-- unpriceable remainder observable instead of silently 0.

CREATE MATERIALIZED VIEW analytics.mv_timer_revenue AS
WITH asset_market AS (
    SELECT DISTINCT ON (s.asset_did)
           s.asset_did, s.asset_identifier, cw.market_bucket
    FROM data_staging.stg_assets s
    LEFT JOIN reference.ref_market_bucket_crosswalk cw
      ON cw.market_signature = reference.market_signature(s.asset_identifier)
    WHERE s.asset_identifier IS NOT NULL
    ORDER BY s.asset_did, s.loaded_at DESC
),
lr_approved AS (
    SELECT DISTINCT asset_did
    FROM data_staging.stg_asset_tasks
    WHERE task_name_clean = 'Live Review Complete' AND task_status = 'approved'
),
tech_task AS (
    SELECT t.asset_did, t.task_clean, t.user_email,
           max(t.user_name) AS user_name,
           sum(t.duration_min) AS tech_minutes,
           count(*) AS timer_entries,
           min(t.start_date) AS first_work_date,
           max(t.end_date) AS last_work_date
    FROM data_staging.stg_timer_activities_clean t
    WHERE t.asset_did IS NOT NULL
      AND t.task_clean IS NOT NULL
      AND t.duration_min > 0
    GROUP BY 1, 2, 3
),
task_total AS (
    SELECT asset_did, task_clean, sum(tech_minutes) AS task_minutes
    FROM tech_task GROUP BY 1, 2
),
base AS (
    SELECT
        tt.asset_did,
        am.asset_identifier,
        am.market_bucket,
        tt.task_clean,
        tt.user_email,
        tt.user_name,
        tt.timer_entries,
        tt.first_work_date,
        tt.last_work_date,
        tt.tech_minutes,
        tot.task_minutes,
        tt.tech_minutes / tot.task_minutes AS tech_share,
        (la.asset_did IS NOT NULL) AS lr_approved,
        r.amount_usd  AS task_rate_usd,
        r.duration_hrs AS task_rate_duration_hrs,
        CASE
            WHEN am.market_bucket IS NULL OR am.market_bucket = 'EXCLUDED'
                 OR r.id IS NULL THEN NULL
            WHEN tt.task_clean = 'Final COP Complete' AND la.asset_did IS NULL
                THEN r.amount_usd + COALESCE(rlr.amount_usd, 0)
            WHEN tt.task_clean = 'Live Review Complete' AND la.asset_did IS NULL
                THEN 0
            ELSE r.amount_usd
        END AS bundled_rate_usd,
        (tt.task_clean = 'Final COP Complete' AND la.asset_did IS NULL
         AND r.id IS NOT NULL) AS lr_bundled_into_fcop,
        CASE
            WHEN am.market_bucket IS NULL THEN 'no_market'
            WHEN am.market_bucket = 'EXCLUDED' THEN 'excluded_market'
            WHEN r.id IS NULL THEN 'no_rate'
            WHEN tt.task_clean = 'Live Review Complete' AND la.asset_did IS NULL
                THEN 'lr_unapproved_zero'
            ELSE 'priced'
        END AS pricing_status
    FROM tech_task tt
    JOIN task_total tot
      ON tot.asset_did = tt.asset_did AND tot.task_clean = tt.task_clean
    LEFT JOIN asset_market am ON am.asset_did = tt.asset_did
    LEFT JOIN lr_approved la ON la.asset_did = tt.asset_did
    LEFT JOIN reference.ref_task_revenue_rates r
      ON r.market_bucket = am.market_bucket AND r.task_name_norm = tt.task_clean
    LEFT JOIN reference.ref_task_revenue_rates rlr
      ON rlr.market_bucket = am.market_bucket
     AND rlr.task_name_norm = 'Live Review Complete'
)
SELECT
    asset_did, asset_identifier, market_bucket, task_clean,
    user_email, user_name, timer_entries, first_work_date, last_work_date,
    round(tech_minutes, 2) AS tech_minutes,
    round(task_minutes, 2) AS task_minutes,
    round(tech_share, 4)   AS tech_share,
    lr_approved, lr_bundled_into_fcop, pricing_status,
    task_rate_usd, task_rate_duration_hrs, bundled_rate_usd,
    round(bundled_rate_usd * tech_share, 2) AS amount_usd
FROM base;

-- Required for REFRESH ... CONCURRENTLY (verified: user_email has no NULLs)
CREATE UNIQUE INDEX mv_timer_revenue_pk
    ON analytics.mv_timer_revenue (asset_did, task_clean, user_email);

CREATE INDEX mv_timer_revenue_email_idx ON analytics.mv_timer_revenue (user_email);
CREATE INDEX mv_timer_revenue_bucket_idx ON analytics.mv_timer_revenue (market_bucket);

GRANT SELECT ON analytics.mv_timer_revenue TO service_role;

-- Add to the refresh whitelist (current live def + one branch)
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
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;
    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$function$;
