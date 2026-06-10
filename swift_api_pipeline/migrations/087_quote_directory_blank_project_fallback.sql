-- 087_quote_directory_blank_project_fallback.sql
-- Directory match: let a directory entry with a BLANK project act as the catch-all
-- fallback for any site sharing its GC/Carrier/Market, when no project-specific entry
-- matches. Previously the match REQUIRED k.project_n <> '' (081), so blank-project rows
-- (e.g. "MasTec / T-Mobile / STX - Overlay", no project) could never match and the site
-- showed no recipient even though the catch-all existed.
--
-- Change vs the live view: ONLY the directory-join WHERE/ORDER BY. project is now a
-- wildcard when blank — GC/Carrier/Market must always be contained, project contained
-- ONLY when the entry specifies one. ORDER BY length DESC (+ project-present tiebreak)
-- keeps a project-specific entry above the blank fallback (most-specific still wins).
-- All columns preserved (incl. 083's service_rate_override / service_rate_overridden).
-- Validated read-only on live: +2 sites gain a recipient, 0 lost, 0 changed.

CREATE OR REPLACE VIEW analytics.v_quote_review AS
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
    (o.service_rate_override IS NOT NULL) AS service_rate_overridden,
    o.updated_by AS override_by, o.updated_at AS override_at,
    m.inv_project AS inv_project_base, m.inv_site_name,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.site_id ELSE m.inv_site_id END AS inv_site_id,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service ELSE m.inv_product_service END AS inv_product_service,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service_type ELSE m.inv_product_service_type END AS inv_product_service_type,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.invoice_category ELSE m.inv_invoice_category END AS inv_invoice_category,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.service_type ELSE m.inv_service_type END AS inv_service_type,
    CASE WHEN opt.line_key IS NOT NULL THEN opt.sow ELSE m.inv_sow END AS inv_sow,
    COALESCE(o.service_rate_override,
      CASE WHEN opt.line_key IS NOT NULL THEN opt.service_rate ELSE m.inv_service_rate END) AS inv_service_rate,
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
  (dir.hit IS NOT NULL)             AS directory_matched,
  COALESCE(dir.has_conflict, false) AS directory_conflict
FROM base
LEFT JOIN LATERAL (
  SELECT ' ' || regexp_replace(upper(
    coalesce(base.asset_id,'')||' '||coalesce(base.gc,'')||' '||coalesce(base.carrier,'')||' '||coalesce(base.market,'')||' '||coalesce(base.project,'')
  ), '[/_\s\-]+', ' ', 'g') || ' ' AS pathstr
) seg ON true
LEFT JOIN LATERAL (
  SELECT k.recipient, k.cc, k.has_conflict, 1 AS hit
  FROM analytics.v_quote_directory_keys k
  WHERE k.gc_n <> '' AND k.carrier_n <> '' AND k.market_n <> ''
    AND position(' '||k.gc_n||' '      IN seg.pathstr) > 0
    AND position(' '||k.carrier_n||' ' IN seg.pathstr) > 0
    AND position(' '||k.market_n||' '  IN seg.pathstr) > 0
    AND (k.project_n = '' OR position(' '||k.project_n||' ' IN seg.pathstr) > 0)
  ORDER BY length(k.gc_n || k.carrier_n || k.market_n || k.project_n) DESC, (k.project_n <> '') DESC
  LIMIT 1
) dir ON true;
GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;