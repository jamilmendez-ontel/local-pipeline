-- 074_quote_invoice_options.sql
-- Manual invoice-line selection for sites with >1 priced line (open accounting
-- Q#1, solved per-entry). Adds:
--   * analytics.mv_quote_invoice_options — every priced invoice line per QP task,
--     keyed by a STABLE content-hash line_key. The surrogate stg_invoicing_form.id
--     churns every run (the table is DELETE+INSERT reloaded), so we hash stable
--     content fields instead; the saved choice survives the daily refresh.
--   * stg_quote_overrides.chosen_line_key — which line the user picked.
--   * v_quote_review now prefers the chosen line's fields over the default "best"
--     line, and exposes invoice_chosen + chosen_line_key.
--
-- DAILY REFRESH (follow-up): also
--   REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_invoice_options;
-- (or plain REFRESH) alongside mv_quote_review.

CREATE MATERIALIZED VIEW analytics.mv_quote_invoice_options AS
WITH wl AS (
  SELECT w.task_did, w.asset_name_norm
  FROM analytics.v_quote_worklist w
  WHERE w.task_name ILIKE '%Quote Provided%' AND w.task_name NOT ILIKE '%Revised FCOP%'
), inv AS (
  SELECT s.form_did, s.site_id, s.project, s.requirement_status, s.sow, s.site_name_norm,
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
    AND NULLIF(regexp_replace(COALESCE(s.service_rate,''),'[^0-9.]','','g'),'') IS NOT NULL
)
SELECT DISTINCT
  wl.task_did,
  md5(concat_ws('|', inv.form_did, inv.site_id, inv.project, inv.service_rate,
                inv.product_service, inv.requirement_status, inv.sow)) AS line_key,
  inv.product_service, inv.product_service_type, inv.invoice_category, inv.service_type,
  inv.service_rate, inv.amount, inv.site_id, inv.project, inv.requirement_status,
  inv.sow, inv.form_did
FROM wl JOIN inv ON inv.site_name_norm = wl.asset_name_norm;

CREATE INDEX mv_quote_invoice_options_task_idx ON analytics.mv_quote_invoice_options (task_did);
GRANT SELECT ON analytics.mv_quote_invoice_options TO anon, authenticated, service_role;

ALTER TABLE data_staging.stg_quote_overrides ADD COLUMN IF NOT EXISTS chosen_line_key text;

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
  m.inv_project AS inv_project_base,
  m.inv_site_name,
  -- chosen line wins WHOLESALE (incl. its NULLs); fall back to default only when
  -- no valid chosen line is in effect (opt did not match, e.g. stale key).
  CASE WHEN opt.line_key IS NOT NULL THEN opt.site_id              ELSE m.inv_site_id              END AS inv_site_id,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service      ELSE m.inv_product_service      END AS inv_product_service,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service_type ELSE m.inv_product_service_type END AS inv_product_service_type,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.invoice_category     ELSE m.inv_invoice_category     END AS inv_invoice_category,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.service_type         ELSE m.inv_service_type         END AS inv_service_type,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.sow                  ELSE m.inv_sow                  END AS inv_sow,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.service_rate         ELSE m.inv_service_rate         END AS inv_service_rate,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.requirement_status   ELSE m.inv_requirement_status   END AS inv_requirement_status,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.form_did             ELSE m.inv_form_did             END AS inv_form_did,
  CASE WHEN opt.line_key IS NOT NULL THEN opt.project              ELSE m.inv_project              END AS inv_project,
  o.chosen_line_key,
  (opt.line_key IS NOT NULL) AS invoice_chosen,
  m.priced_line_count, m.status
FROM analytics.mv_quote_review m
LEFT JOIN data_staging.stg_quote_overrides o USING (task_did)
LEFT JOIN analytics.mv_quote_invoice_options opt
  ON opt.task_did = m.task_did AND opt.line_key = o.chosen_line_key;
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
