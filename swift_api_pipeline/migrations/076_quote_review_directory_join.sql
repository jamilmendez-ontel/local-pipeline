-- 076_quote_review_directory_join.sql
-- Add Recipient + CC to v_quote_review by joining the Quote Directory on the
-- entry's EFFECTIVE (override-aware) GC|Carrier|Market|Project key. Wraps the
-- prior definition in a `base` CTE so the join can key off the effective columns.
-- LATERAL + LIMIT 1 picks one directory row per key (handles the few dup keys).
-- Adds: quote_recipient, quote_cc, directory_matched. Match rate (2026-06-09): 208/322.

DROP VIEW IF EXISTS analytics.v_quote_review;
CREATE VIEW analytics.v_quote_review AS
WITH base AS (
  SELECT
    m.task_did, m.asset_id, m.asset_name, m.task_name, m.task_status,
    COALESCE(o.subcon,  m.subcon)  AS subcon,
    COALESCE(o.gc,      m.gc)      AS gc,
    COALESCE(o.carrier, m.carrier) AS carrier,
    COALESCE(o.market,  m.market)  AS market,
    COALESCE(o.project, m.project) AS project,
    COALESCE(o.fuze_id, m.fuze_id) AS fuze_id,
    (m.needs_review AND NOT COALESCE(o.verified, false)) AS needs_review,
    m.needs_review AS needs_review_base,
    COALESCE(o.verified, false) AS verified,
    o.verified_by, o.verified_at,
    (o.subcon IS NOT NULL) AS subcon_overridden,
    (o.gc IS NOT NULL) AS gc_overridden,
    (o.carrier IS NOT NULL) AS carrier_overridden,
    (o.market IS NOT NULL) AS market_overridden,
    (o.project IS NOT NULL) AS project_overridden,
    (o.fuze_id IS NOT NULL) AS fuze_id_overridden,
    o.updated_by AS override_by, o.updated_at AS override_at,
    m.inv_project AS inv_project_base, m.inv_site_name,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.site_id ELSE m.inv_site_id END AS inv_site_id,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service ELSE m.inv_product_service END AS inv_product_service,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service_type ELSE m.inv_product_service_type END AS inv_product_service_type,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.invoice_category ELSE m.inv_invoice_category END AS inv_invoice_category,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.service_type ELSE m.inv_service_type END AS inv_service_type,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.sow ELSE m.inv_sow END AS inv_sow,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.service_rate ELSE m.inv_service_rate END AS inv_service_rate,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.requirement_status ELSE m.inv_requirement_status END AS inv_requirement_status,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.form_did ELSE m.inv_form_did END AS inv_form_did,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.project ELSE m.inv_project END AS inv_project,
    o.chosen_line_key,
    (opt.line_key IS NOT NULL) AS invoice_chosen,
    m.priced_line_count, m.status
  FROM analytics.mv_quote_review m
  LEFT JOIN data_staging.stg_quote_overrides o USING (task_did)
  LEFT JOIN analytics.mv_quote_invoice_options opt
    ON opt.task_did = m.task_did AND opt.line_key = o.chosen_line_key
)
SELECT base.*,
  dir.recipient AS quote_recipient,
  dir.cc        AS quote_cc,
  (dir.match_key IS NOT NULL) AS directory_matched
FROM base
LEFT JOIN LATERAL (
  SELECT d.recipient, d.cc, d.match_key
  FROM reference.ref_quote_directory d
  WHERE d.match_key =
    upper(regexp_replace(trim(coalesce(base.gc,'')),'\s+',' ','g'))||'|'||
    upper(regexp_replace(trim(coalesce(base.carrier,'')),'\s+',' ','g'))||'|'||
    upper(regexp_replace(trim(coalesce(base.market,'')),'\s+',' ','g'))||'|'||
    upper(regexp_replace(trim(coalesce(base.project,'')),'\s+',' ','g'))
  ORDER BY d.id
  LIMIT 1
) dir ON true;
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
