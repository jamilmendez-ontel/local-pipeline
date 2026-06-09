-- 072_quote_overrides.sql
-- Manual category overrides for the quote webapp (Subcon/GC/Carrier/Market/Scope/
-- Fuze ID). Accounting can re-point any of these to a segment of the Site-ID path,
-- set it blank, or clear it back to the auto-parse. Persisted + shared.
--
-- Design: overrides live in their OWN table (outside the materialized base read
-- model) so they apply at request time and survive the daily MV refresh. The MV
-- is renamed to analytics.mv_quote_review (the daily-refreshed base); a live VIEW
-- analytics.v_quote_review = base MV LEFT JOIN overrides, override-wins via
-- COALESCE. An empty-string override shows blank but still counts as overridden
-- (o.<col> IS NOT NULL); NULL = no override (fall back to parse).
--
-- DAILY REFRESH (follow-up): wire
--   REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_review;
-- (renamed target) into the pipeline tail. Overrides are untouched by refresh.

CREATE TABLE IF NOT EXISTS data_staging.stg_quote_overrides (
  task_did   text PRIMARY KEY,
  subcon     text,
  gc         text,
  carrier    text,
  market     text,
  project    text,
  fuze_id    text,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_overrides TO service_role;

ALTER MATERIALIZED VIEW analytics.v_quote_review RENAME TO mv_quote_review;

CREATE VIEW analytics.v_quote_review AS
SELECT
  m.task_did, m.asset_id, m.asset_name, m.task_name, m.task_status,
  COALESCE(o.subcon,  m.subcon)  AS subcon,
  COALESCE(o.gc,      m.gc)      AS gc,
  COALESCE(o.carrier, m.carrier) AS carrier,
  COALESCE(o.market,  m.market)  AS market,
  COALESCE(o.project, m.project) AS project,
  COALESCE(o.fuze_id, m.fuze_id) AS fuze_id,
  m.needs_review,
  (o.subcon  IS NOT NULL) AS subcon_overridden,
  (o.gc      IS NOT NULL) AS gc_overridden,
  (o.carrier IS NOT NULL) AS carrier_overridden,
  (o.market  IS NOT NULL) AS market_overridden,
  (o.project IS NOT NULL) AS project_overridden,
  (o.fuze_id IS NOT NULL) AS fuze_id_overridden,
  o.updated_by AS override_by,
  o.updated_at AS override_at,
  m.inv_project, m.inv_site_name, m.inv_site_id, m.inv_product_service,
  m.inv_product_service_type, m.inv_invoice_category, m.inv_service_type,
  m.inv_sow, m.inv_service_rate, m.inv_requirement_status, m.inv_form_did,
  m.priced_line_count, m.status
FROM analytics.mv_quote_review m
LEFT JOIN data_staging.stg_quote_overrides o USING (task_did);
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
