-- 070_quote_review_materialize.sql
-- Convert analytics.v_quote_review (created in 069) from a plain VIEW to a
-- MATERIALIZED VIEW. The plain view's LATERAL + aggregate joins over
-- stg_invoicing_form (~317k rows) exceed the PostgREST role statement timeout on
-- live reads (the webapp reads it via PostgREST on every page load). The data
-- changes once daily, so we materialize it: compute on refresh, read instantly.
--
-- IMPORTANT — daily refresh wiring (follow-up): add
--   REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.v_quote_review;
-- to the tail of the asset-tasks + invoicing pipelines (after stg_asset_tasks and
-- stg_invoicing_form reload), alongside the other analytics MV refreshes. The
-- UNIQUE INDEX below is what allows CONCURRENTLY. Until wired, refresh manually.
--
-- Definition is identical to 069 (see that file for column/status notes).
-- Verified live 2026-06-08: 322 rows, PostgREST read instant, count endpoint OK.

DROP VIEW IF EXISTS analytics.v_quote_review;

CREATE MATERIALIZED VIEW analytics.v_quote_review AS
WITH wl AS (
  SELECT e.task_did, e.asset_id, e.asset_name, e.task_name, e.task_status,
         e.subcon, e.gc, e.carrier, e.market, e.project, e.fuze_id, e.needs_review,
         w.asset_name_norm
  FROM analytics.v_quote_worklist_enriched e
  JOIN analytics.v_quote_worklist w USING (task_did)
  WHERE e.task_name ILIKE '%Quote Provided%'
    AND e.task_name NOT ILIKE '%Revised FCOP%'
), inv_lines AS (
  SELECT
    s.form_did, s.project, s.site_name, s.site_id, s.site_name_norm,
    s.requirement_status, s.invoice_category, s.sow,
    s.extra_fields->>'Service Type' AS service_type,
    COALESCE(NULLIF(s.invoice_category,''), NULLIF(s.extra_fields->>'Service Type','')) AS product_service,
    s.service_rate,
    NULLIF(regexp_replace(COALESCE(s.service_rate,''),'[^0-9.]','','g'),'')::numeric AS amount
  FROM data_staging.stg_invoicing_form s
  WHERE s.task ILIKE '%Quote Provided%' AND s.task NOT ILIKE '%Revised FCOP%'
), best AS (
  SELECT wl.*,
         inv.project            AS inv_project,
         inv.site_name          AS inv_site_name,
         inv.site_id            AS inv_site_id,
         inv.product_service    AS inv_product_service,
         inv.invoice_category   AS inv_invoice_category,
         inv.service_type       AS inv_service_type,
         inv.sow                AS inv_sow,
         inv.service_rate       AS inv_service_rate,
         inv.requirement_status AS inv_requirement_status,
         inv.form_did           AS inv_form_did
  FROM wl
  LEFT JOIN LATERAL (
    SELECT i.*
    FROM inv_lines i
    WHERE i.site_name_norm = wl.asset_name_norm
    ORDER BY (i.amount IS NOT NULL) DESC, i.site_id
    LIMIT 1
  ) inv ON true
), cnt AS (
  SELECT wl.task_did,
         count(i.*) FILTER (WHERE i.amount IS NOT NULL) AS priced_line_count,
         count(i.*) AS any_line_count
  FROM wl
  LEFT JOIN inv_lines i ON i.site_name_norm = wl.asset_name_norm
  GROUP BY wl.task_did
)
SELECT
  b.task_did, b.asset_id, b.asset_name, b.task_name, b.task_status,
  b.subcon, b.gc, b.carrier, b.market, b.project, b.fuze_id, b.needs_review,
  b.inv_project, b.inv_site_name, b.inv_site_id, b.inv_product_service,
  b.inv_invoice_category, b.inv_service_type, b.inv_sow, b.inv_service_rate,
  b.inv_requirement_status, b.inv_form_did,
  CASE b.inv_form_did
    WHEN '-NnoFijdV83f4LCm6Ktr' THEN 'Invoicing Form A (legacy)'
    WHEN '-OmoXCo93LkiEzTrsVDy' THEN 'Invoicing Form B'
    WHEN '-O_NzUh9FPjw2Sgr3ztS' THEN 'Invoicing Form C'
    WHEN '-ONLRetis86GmTN0_TFm' THEN 'Invoicing Form D'
    ELSE b.inv_form_did
  END AS source_form,
  c.priced_line_count,
  CASE WHEN c.priced_line_count > 0 THEN 'ready'
       WHEN c.any_line_count > 0    THEN 'no_price'
       ELSE 'no_match' END AS status
FROM best b
JOIN cnt c USING (task_did);

CREATE UNIQUE INDEX mv_quote_review_task_did_idx ON analytics.v_quote_review (task_did);
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
