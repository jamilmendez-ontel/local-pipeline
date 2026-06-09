-- 071_quote_review_psvc_type_and_project.sql
-- Two refinements to the analytics.v_quote_review materialized view:
--  1. ADD inv_product_service_type = 'Invoice Category' | 'Service Type' | NULL,
--     so the UI can show WHICH field the Product/Service value came from.
--     (Legacy form A fills Invoice Category; forms B/C/D fill Service Type.)
--  2. DROP the invented source_form A/B/C/D label. A single Swift invoicing form
--     spans multiple TS projects (e.g. -ONLRetis... contains TS16/17/18), so a
--     form-level label is misleading. The truthful per-entry source is the invoice
--     row's Project (inv_project, e.g. 'TECH-OPS: TS18'); inv_form_did is kept for
--     raw traceability.
-- Rebuilds the MV (CREATE OR REPLACE is not available for materialized views).

DROP MATERIALIZED VIEW IF EXISTS analytics.v_quote_review;

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
    CASE WHEN NULLIF(s.invoice_category,'') IS NOT NULL THEN 'Invoice Category'
         WHEN NULLIF(s.extra_fields->>'Service Type','') IS NOT NULL THEN 'Service Type'
         ELSE NULL END AS product_service_type,
    s.service_rate,
    NULLIF(regexp_replace(COALESCE(s.service_rate,''),'[^0-9.]','','g'),'')::numeric AS amount
  FROM data_staging.stg_invoicing_form s
  WHERE s.task ILIKE '%Quote Provided%' AND s.task NOT ILIKE '%Revised FCOP%'
), best AS (
  SELECT wl.*,
         inv.project              AS inv_project,
         inv.site_name            AS inv_site_name,
         inv.site_id              AS inv_site_id,
         inv.product_service      AS inv_product_service,
         inv.product_service_type AS inv_product_service_type,
         inv.invoice_category     AS inv_invoice_category,
         inv.service_type         AS inv_service_type,
         inv.sow                  AS inv_sow,
         inv.service_rate         AS inv_service_rate,
         inv.requirement_status   AS inv_requirement_status,
         inv.form_did             AS inv_form_did
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
  b.inv_product_service_type, b.inv_invoice_category, b.inv_service_type,
  b.inv_sow, b.inv_service_rate, b.inv_requirement_status, b.inv_form_did,
  c.priced_line_count,
  CASE WHEN c.priced_line_count > 0 THEN 'ready'
       WHEN c.any_line_count > 0    THEN 'no_price'
       ELSE 'no_match' END AS status
FROM best b
JOIN cnt c USING (task_did);

CREATE UNIQUE INDEX mv_quote_review_task_did_idx ON analytics.v_quote_review (task_did);
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
