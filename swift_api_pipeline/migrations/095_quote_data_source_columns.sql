-- 095_quote_data_source_columns.sql
-- Applied live 2026-06-11 via Supabase MCP apply_migration.
--
-- Richer columns for the quote-automation app's Data Source tab.
--
-- (1) analytics.v_quote_source_asset_tasks (NEW plain view): the enriched
--     Quote-Provided worklist (Subcon/GC/Carrier/Market/Scope/Fuze ID from
--     v_quote_worklist_enriched) + Organization (org_name) and Project
--     (project_name, e.g. "TECH-OPS: TS18") from analytics.v_asset_tasks.
--     270 rows. (Milestone is NOT available — the asset-tasks extractor pulls a
--     flat report projection that omits the nested task.milestone; capturing it
--     is a separate extractor change.)
--
-- (2) analytics.mv_quote_source_invoice_lines (RECREATED): same QP-worklist →
--     invoicing-row join (site_name_norm = asset_name_norm) as migration 090,
--     extended with site_name, Pricing Type / Service Type (Others) from
--     extra_fields, and ll_cop / landlord / landlord_others / pmi_cop /
--     rf_mitigation_cop. The DISTINCT row_key hash now folds in the added
--     columns so it stays unique (validated read-only: 330 rows = 330 keys),
--     keeping the UNIQUE index legal for REFRESH ... CONCURRENTLY. Still wired
--     into analytics.refresh_one_mv + transform.refresh_quote_mvs by name.

-- ── (1) Asset Tasks source view ────────────────────────────────
CREATE OR REPLACE VIEW analytics.v_quote_source_asset_tasks AS
SELECT
  e.task_did,
  t.org_name,
  t.project_name,
  e.asset_name,
  e.asset_id,
  e.task_name,
  e.subcon,
  e.gc,
  e.carrier,
  e.market,
  e.project,
  e.fuze_id
FROM analytics.v_quote_worklist_enriched e
LEFT JOIN analytics.v_asset_tasks t USING (task_did);

GRANT SELECT ON analytics.v_quote_source_asset_tasks TO anon, authenticated, service_role;

-- ── (2) Invoicing Rows source MV (recreate with extra columns) ──
DROP MATERIALIZED VIEW IF EXISTS analytics.mv_quote_source_invoice_lines;

CREATE MATERIALIZED VIEW analytics.mv_quote_source_invoice_lines AS
WITH wl AS (
  SELECT w.task_did, w.asset_name, w.asset_name_norm
  FROM analytics.v_quote_worklist w
  WHERE w.task_name ILIKE '%Quote Provided%' AND w.task_name NOT ILIKE '%Revised FCOP%'
), inv AS (
  SELECT s.form_did, s.site_id, s.project, s.requirement_status, s.sow, s.site_name_norm, s.task,
         s.invoice_category, s.site_name,
         s.extra_fields ->> 'Service Type'          AS service_type,
         s.extra_fields ->> 'Pricing Type'          AS pricing_type,
         s.extra_fields ->> 'Service Type (Others)' AS service_type_others,
         s.service_rate, s.ll_cop, s.landlord, s.landlord_others, s.pmi_cop, s.rf_mitigation_cop,
         COALESCE(NULLIF(s.invoice_category, ''), NULLIF(s.extra_fields ->> 'Service Type', '')) AS product_service,
         CASE
           WHEN NULLIF(s.invoice_category, '') IS NOT NULL THEN 'Invoice Category'
           WHEN NULLIF(s.extra_fields ->> 'Service Type', '') IS NOT NULL THEN 'Service Type'
           ELSE NULL
         END AS product_service_type,
         NULLIF(regexp_replace(COALESCE(s.service_rate, ''), '[^0-9.]', '', 'g'), '')::numeric AS amount
  FROM data_staging.stg_invoicing_form s
  WHERE s.task ILIKE '%Quote Provided%' AND s.task NOT ILIKE '%Revised FCOP%'
)
SELECT DISTINCT
  wl.task_did, wl.asset_name,
  inv.project, inv.site_name, inv.site_id, inv.sow,
  inv.pricing_type, inv.service_type, inv.service_type_others, inv.service_rate,
  inv.ll_cop, inv.landlord, inv.landlord_others, inv.pmi_cop, inv.rf_mitigation_cop,
  inv.requirement_status, inv.task, inv.invoice_category,
  inv.product_service, inv.product_service_type,
  inv.amount, (inv.amount IS NOT NULL) AS priced, inv.form_did,
  md5(concat_ws('|',
    COALESCE(wl.task_did,''), COALESCE(inv.form_did,''), COALESCE(inv.site_id,''),
    COALESCE(inv.project,''), COALESCE(inv.task,''), COALESCE(inv.requirement_status,''),
    COALESCE(inv.invoice_category,''), COALESCE(inv.service_type,''), COALESCE(inv.sow,''),
    COALESCE(inv.service_rate,''), COALESCE(inv.site_name,''), COALESCE(inv.pricing_type,''),
    COALESCE(inv.service_type_others,''), COALESCE(inv.ll_cop,''), COALESCE(inv.landlord,''),
    COALESCE(inv.landlord_others,''), COALESCE(inv.pmi_cop,''), COALESCE(inv.rf_mitigation_cop,''))) AS row_key
FROM wl
JOIN inv ON inv.site_name_norm = wl.asset_name_norm;

CREATE UNIQUE INDEX mv_quote_source_invoice_lines_row_key_idx
  ON analytics.mv_quote_source_invoice_lines (row_key);

GRANT SELECT ON analytics.mv_quote_source_invoice_lines TO anon, authenticated, service_role;
