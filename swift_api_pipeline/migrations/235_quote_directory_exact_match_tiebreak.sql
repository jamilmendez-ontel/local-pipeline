-- 235_quote_directory_exact_match_tiebreak.sql
-- Directory match: prefer a directory entry whose STRUCTURED key (gc/carrier/market,
-- project blank = catch-all) exactly equals the quote's effective (override-coalesced)
-- fields, before falling back to the 081 longest-phrase-in-path ranking.
--
-- Why: the phrase match scans pathstr = asset_id || gc || carrier || market || project,
-- and Swift asset_ids can embed BOTH parties' GC names (e.g. "Carr & Duff/AMP
-- Communications/VZW/Tri-State/Small Cell/..."). With length-DESC ordering alone, the
-- longer foreign GC key ("AMP COMMUNICATIONS...", 40 chars) permanently outranked the
-- quote's own GC key ("CARR & DUFF...", 33 chars), so saving the correct recipients in
-- the directory never changed the displayed recipient (2026-08-14, PHI Eastern State
-- Penn 05 SC).
--
-- Change vs the live view: ONLY the dir lateral's ORDER BY gains two leading clauses:
--   1. full structured-key equality (gc+carrier+market, project exact or blank) first;
--   2. then gc-only equality as a fallback;
--   3. then the existing length DESC + project-present tiebreak, unchanged.
-- Among two exact entries the length clause still ranks project-specific above the
-- blank-project catch-all. quote_norm is IMMUTABLE SQL; cost is negligible.
--
-- NOTE: body is taken from the LIVE pg_get_viewdef (not migration 087): the live view
-- gained product_service_override / line_items columns and reads app_quote.overrides
-- (tables renamed by 111/116 without redefining the view, which is OID-bound).
-- Validated read-only on live before applying: exactly 1 of 22 worklist rows changes
-- (PHI Eastern State Penn 05 SC: AMP -> Carr & Duff recipients), 0 lose a match.
-- APPLIED to cloud (voqfjfngdpcvevbkikud) via MCP apply_migration 2026-08-14 ~04:3x ET.
-- Post-apply verification: 22/22 worklist rows directory_matched, 0 conflicts, PHI task
-- resolves rhughes/mdalessio/kjacquel/jthompson @carrduff.com, all 9 AMP-GC quotes
-- resolve AMP recipients. Companion data cleanup (not in this file, applied same
-- session): ref_quote_directory duplicate rows 285-287 deleted (284 kept), row 37 gc
-- restored 'AMP Communication' -> 'AMP Communications' (canon-equal to row 288, groups
-- without conflict).

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
    COALESCE(o.product_service_override,
      CASE WHEN opt.line_key IS NOT NULL THEN opt.product_service ELSE m.inv_product_service END) AS inv_product_service,
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
    m.priced_line_count, m.status,
    (o.product_service_override IS NOT NULL) AS product_service_overridden,
    o.line_items
  FROM analytics.mv_quote_review m
  LEFT JOIN app_quote.overrides o USING (task_did)
  LEFT JOIN analytics.mv_quote_invoice_options opt
    ON opt.task_did = m.task_did AND opt.line_key = o.chosen_line_key
)
SELECT
  base.task_did, base.asset_id, base.asset_name, base.task_name, base.task_status,
  base.subcon, base.gc, base.carrier, base.market, base.project, base.fuze_id,
  base.needs_review, base.needs_review_base, base.verified, base.verified_by, base.verified_at,
  base.subcon_overridden, base.gc_overridden, base.carrier_overridden, base.market_overridden,
  base.project_overridden, base.fuze_id_overridden, base.service_rate_overridden,
  base.override_by, base.override_at,
  base.inv_project_base, base.inv_site_name, base.inv_site_id,
  base.inv_product_service, base.inv_product_service_type, base.inv_invoice_category,
  base.inv_service_type, base.inv_sow, base.inv_service_rate, base.inv_requirement_status,
  base.inv_form_did, base.inv_project,
  base.chosen_line_key, base.invoice_chosen, base.priced_line_count,
  CASE
    WHEN base.status = 'no_match' THEN 'no_match'
    WHEN COALESCE(NULLIF(regexp_replace(COALESCE(base.inv_service_rate, ''), '[^0-9.]', '', 'g'), '')::numeric, 0) > 0 THEN 'ready'
    WHEN base.line_items IS NOT NULL AND jsonb_array_length(base.line_items) > 0 THEN 'ready'
    ELSE 'no_price'
  END AS status,
  dir.recipient AS quote_recipient,
  dir.cc        AS quote_cc,
  (dir.hit IS NOT NULL)             AS directory_matched,
  COALESCE(dir.has_conflict, false) AS directory_conflict,
  base.product_service_overridden,
  base.line_items
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
  ORDER BY
    (    k.gc_n      = analytics.quote_norm(coalesce(base.gc, ''))
     AND k.carrier_n = analytics.quote_norm(coalesce(base.carrier, ''))
     AND k.market_n  = analytics.quote_norm(coalesce(base.market, ''))
     AND (k.project_n = '' OR k.project_n = analytics.quote_norm(coalesce(base.project, '')))
    ) DESC,
    (k.gc_n = analytics.quote_norm(coalesce(base.gc, ''))) DESC,
    length(k.gc_n || k.carrier_n || k.market_n || k.project_n) DESC,
    (k.project_n <> '') DESC
  LIMIT 1
) dir ON true;

GRANT SELECT ON analytics.v_quote_review TO anon, authenticated, service_role;
