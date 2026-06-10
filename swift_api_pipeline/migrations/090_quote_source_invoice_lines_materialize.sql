-- 090_quote_source_invoice_lines_materialize.sql
--
-- The quote-automation Data Source tab read analytics.v_quote_source_invoice_lines
-- (a PLAIN view), which re-ran its join over the ~320k-row stg_invoicing_form on
-- every page load (~1.6s even after the 089 worklist-index fix). The invoicing
-- data only changes once a day (nightly invoicing pipeline), so recomputing per
-- read buys zero freshness. Materialize it like mv_quote_invoice_options: read is
-- then a few ms, and it refreshes once per pipeline run.
--
-- Uniqueness for REFRESH ... CONCURRENTLY: the result is SELECT DISTINCT with no
-- natural key, so we add a content-hash row_key over the independent source
-- columns (COALESCE each to '' so NULLs keep their position and can't collide),
-- mirroring mv_quote_invoice_options.line_key.
--
-- Idempotent-ish: drops the old view (nothing in the DB depends on it, verified
-- via pg_depend) and the MV if present, recreates, and adds the refresh branch.

DROP MATERIALIZED VIEW IF EXISTS analytics.mv_quote_source_invoice_lines;
DROP VIEW IF EXISTS analytics.v_quote_source_invoice_lines;

CREATE MATERIALIZED VIEW analytics.mv_quote_source_invoice_lines AS
WITH wl AS (
  SELECT w.task_did, w.asset_name, w.asset_name_norm
  FROM analytics.v_quote_worklist w
  WHERE w.task_name ILIKE '%Quote Provided%' AND w.task_name NOT ILIKE '%Revised FCOP%'
), inv AS (
  SELECT s.form_did, s.site_id, s.project, s.requirement_status, s.sow, s.site_name_norm, s.task,
         s.invoice_category,
         s.extra_fields->>'Service Type' AS service_type,
         COALESCE(NULLIF(s.invoice_category,''), NULLIF(s.extra_fields->>'Service Type','')) AS product_service,
         CASE WHEN NULLIF(s.invoice_category,'') IS NOT NULL THEN 'Invoice Category'
              WHEN NULLIF(s.extra_fields->>'Service Type','') IS NOT NULL THEN 'Service Type'
              ELSE NULL END AS product_service_type,
         s.service_rate,
         NULLIF(regexp_replace(COALESCE(s.service_rate,''),'[^0-9.]','','g'),'')::numeric AS amount
  FROM data_staging.stg_invoicing_form s
  WHERE s.task ILIKE '%Quote Provided%' AND s.task NOT ILIKE '%Revised FCOP%'
)
SELECT DISTINCT
  wl.task_did,
  wl.asset_name,
  inv.site_id,
  inv.project,
  inv.task,
  inv.requirement_status,
  inv.product_service,
  inv.product_service_type,
  inv.invoice_category,
  inv.service_type,
  inv.sow,
  inv.service_rate,
  inv.amount,
  (inv.amount IS NOT NULL) AS priced,
  inv.form_did,
  md5(concat_ws('|',
        COALESCE(wl.task_did,''), COALESCE(inv.form_did,''), COALESCE(inv.site_id,''),
        COALESCE(inv.project,''), COALESCE(inv.task,''), COALESCE(inv.requirement_status,''),
        COALESCE(inv.invoice_category,''), COALESCE(inv.service_type,''),
        COALESCE(inv.sow,''), COALESCE(inv.service_rate,''))) AS row_key
FROM wl JOIN inv ON inv.site_name_norm = wl.asset_name_norm;

CREATE UNIQUE INDEX mv_quote_source_invoice_lines_uniq_idx
  ON analytics.mv_quote_source_invoice_lines (row_key);
CREATE INDEX mv_quote_source_invoice_lines_task_idx
  ON analytics.mv_quote_source_invoice_lines (task_did);
GRANT SELECT ON analytics.mv_quote_source_invoice_lines TO anon, authenticated, service_role;

-- Teach refresh_one_mv about the new MV (full re-create with the added branch).
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
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;
    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$function$;
