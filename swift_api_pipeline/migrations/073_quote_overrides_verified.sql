-- 073_quote_overrides_verified.sql
-- Add a "verified" flag to the quote overrides. When a human confirms a
-- needs-review entry, the EFFECTIVE needs_review becomes false (the ⚠ badge in the
-- list + the warning in the detail clear, and the "N need review" count drops).
-- We still expose needs_review_base (the raw parse flag) so the Verify/Undo control
-- can render even after verification.

ALTER TABLE data_staging.stg_quote_overrides
  ADD COLUMN IF NOT EXISTS verified    boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS verified_by text,
  ADD COLUMN IF NOT EXISTS verified_at timestamptz;

DROP VIEW IF EXISTS analytics.v_quote_review;
CREATE VIEW analytics.v_quote_review AS
SELECT
  m.task_did, m.asset_id, m.asset_name, m.task_name, m.task_status,
  COALESCE(o.subcon,  m.subcon)  AS subcon,
  COALESCE(o.gc,      m.gc)      AS gc,
  COALESCE(o.carrier, m.carrier) AS carrier,
  COALESCE(o.market,  m.market)  AS market,
  COALESCE(o.project, m.project) AS project,
  COALESCE(o.fuze_id, m.fuze_id) AS fuze_id,
  (m.needs_review AND NOT COALESCE(o.verified, false)) AS needs_review,
  m.needs_review                 AS needs_review_base,
  COALESCE(o.verified, false)    AS verified,
  o.verified_by, o.verified_at,
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
