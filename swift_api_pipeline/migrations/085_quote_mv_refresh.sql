-- 085_quote_mv_refresh.sql
--
-- Wire the two Quote Automation materialized views into the daily refresh path
-- so the quote-automation app never serves stale data.
--
-- The app reads analytics.v_quote_review (a live view = mv_quote_review LEFT JOIN
-- the overrides table) and the per-task invoice options from
-- analytics.mv_quote_invoice_options. Both MVs are derived from
-- data_staging.stg_asset_tasks (worklist) + data_staging.stg_invoicing_form
-- (priced lines), so they go stale whenever either source reloads.
--
-- This migration makes them refreshable via the existing analytics.refresh_one_mv
-- RPC (SECURITY DEFINER, 300s timeout, CONCURRENTLY), the same mechanism the
-- three Ontel core MVs use. transform.refresh_quote_mvs() calls this RPC and is
-- wired into run_analytics_refresh() (nightly --pipeline analytics) and
-- run_invoicing_pipeline().
--
-- Two changes:
--   1. mv_quote_invoice_options had only a NON-unique index on (task_did), so
--      REFRESH ... CONCURRENTLY would fail ("cannot refresh ... concurrently").
--      Replace it with a UNIQUE index on (task_did, line_key) — verified unique
--      (280/280 distinct, 0 NULLs) since task_did can repeat (multi-line sites)
--      but the content-hash line_key disambiguates. mv_quote_review already has
--      its unique index on (task_did).
--   2. Add mv_quote_review + mv_quote_invoice_options branches to refresh_one_mv.
--
-- Idempotent. No data change; index swap + function replace only.

-- 1. Unique index for CONCURRENTLY refresh of mv_quote_invoice_options
DROP INDEX IF EXISTS analytics.mv_quote_invoice_options_task_idx;
CREATE UNIQUE INDEX IF NOT EXISTS mv_quote_invoice_options_uniq_idx
    ON analytics.mv_quote_invoice_options (task_did, line_key);

-- 2. Teach refresh_one_mv about the two quote MVs
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
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;
    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$function$;
