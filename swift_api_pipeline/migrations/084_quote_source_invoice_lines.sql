-- 084_quote_source_invoice_lines.sql
--
-- Data Source tab: every invoicing-form row that REFERENCES a Quote-Provided
-- worklist asset task, INCLUDING unpriced rows.
--
-- Built on data_staging.stg_invoicing_form (NOT analytics.v_quote_invoice_lines,
-- which is priced-only). Same join + inv shaping as analytics.mv_quote_invoice_options
-- (match on site_name_norm = asset_name_norm, Quote-Provided invoice rows,
-- excluding Revised FCOP) but WITHOUT the priced-only filter. That lets the UI
-- show *why* an asset isn't quotable:
--   * no_price -> referencing rows exist but amount IS NULL (blank Service Rate)
--   * no_match -> the asset has no referencing rows at all
-- Verified: the priced subset (280 rows) equals mv_quote_invoice_options; the
-- view adds 56 unpriced rows; 280 of 282 QP worklist tasks have >=1 row.
--
-- Leaf view, read-only, no dependents.

DROP VIEW IF EXISTS analytics.v_quote_source_invoice_lines;

CREATE VIEW analytics.v_quote_source_invoice_lines AS
WITH wl AS (
  SELECT task_did, asset_name, asset_name_norm
  FROM analytics.v_quote_worklist
  WHERE task_name ILIKE '%Quote Provided%'
    AND task_name NOT ILIKE '%Revised FCOP%'
), inv AS (
  SELECT
    s.form_did, s.site_id, s.project, s.requirement_status, s.sow,
    s.site_name_norm, s.task, s.invoice_category,
    (s.extra_fields ->> 'Service Type') AS service_type,
    COALESCE(NULLIF(s.invoice_category, ''), NULLIF(s.extra_fields ->> 'Service Type', '')) AS product_service,
    CASE
      WHEN NULLIF(s.invoice_category, '') IS NOT NULL THEN 'Invoice Category'
      WHEN NULLIF(s.extra_fields ->> 'Service Type', '') IS NOT NULL THEN 'Service Type'
      ELSE NULL
    END AS product_service_type,
    s.service_rate,
    (NULLIF(regexp_replace(COALESCE(s.service_rate, ''), '[^0-9.]', '', 'g'), ''))::numeric AS amount
  FROM data_staging.stg_invoicing_form s
  WHERE s.task ILIKE '%Quote Provided%'
    AND s.task NOT ILIKE '%Revised FCOP%'
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
  inv.form_did
FROM wl
JOIN inv ON inv.site_name_norm = wl.asset_name_norm;

GRANT SELECT ON analytics.v_quote_source_invoice_lines TO anon, authenticated, service_role;
