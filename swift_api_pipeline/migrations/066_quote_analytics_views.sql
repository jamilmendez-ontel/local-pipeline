-- 066_quote_analytics_views.sql
-- Quote Automation Phase 0: report-scoped + joined views. Filtering lives here,
-- never in staging. The join key (FA# primary, name fallback) lives only in
-- v_quote_candidates so it can be iterated without re-extract.
--
-- Cross-form note: live data has TWO invoicing-form schemas. One form
-- (-NnoFijdV83f4LCm6Ktr) carries "Invoice Category"; the other three carry
-- "Service Type" (in extra_fields) + "Site Name / Location Name". "Service Rate"
-- (price) and "Site ID" (FA#) are universal. The product/service label is
-- COALESCEd across both here in the analytics layer.

-- Priced, quotable invoice lines (Quote Provided + PO Issued).
CREATE OR REPLACE VIEW analytics.v_quote_invoice_lines AS
SELECT
    form_did, project, site_name, site_id, fa_number, site_name_norm,
    task, requirement_status,
    invoice_category,
    extra_fields->>'Service Type' AS service_type,
    -- unified label for the quote line (Phase 1 may refine using the (Others) fields)
    COALESCE(NULLIF(invoice_category, ''), NULLIF(extra_fields->>'Service Type', '')) AS product_service,
    sow, service_rate,
    NULLIF(REGEXP_REPLACE(COALESCE(service_rate, ''), '[^0-9.]', '', 'g'), '')::numeric AS amount,
    ll_cop, landlord, landlord_others, pmi_cop, rf_mitigation_cop,
    -- secondary descriptive name carried by the newer forms (normalized for name-fallback joins)
    NULLIF(UPPER(REGEXP_REPLACE(TRIM(COALESCE(extra_fields->>'Site Name / Location Name', '')), '\s+', ' ', 'g')), '') AS location_name_norm
FROM data_staging.stg_invoicing_form
WHERE (task ILIKE '%Quote Provided%' OR task ILIKE '%PO Issued%')
  AND NULLIF(REGEXP_REPLACE(COALESCE(service_rate, ''), '[^0-9.]', '', 'g'), '') IS NOT NULL;

-- Accounting-assigned Quote-Provided / PO-Issued worklist.
CREATE OR REPLACE VIEW analytics.v_quote_worklist AS
SELECT
    t.task_did, t.project_did, t.asset_id, t.asset_name, t.task_name, t.task_status,
    t.task_assigned_to_name,
    (SELECT mm[1]
       FROM regexp_matches(COALESCE(t.asset_id, ''), '(\d{6,9})', 'g') AS mm
      ORDER BY length(mm[1]) DESC, mm[1]
      LIMIT 1) AS fa_number,
    NULLIF(UPPER(REGEXP_REPLACE(TRIM(COALESCE(t.asset_name, '')), '\s+', ' ', 'g')), '') AS asset_name_norm
FROM data_staging.stg_asset_tasks t
WHERE t.task_name IN (
        '7. Quote Provided', '8. PO Issued',
        '11. Revised FCOP Quote Provided', '12. Revised FCOP Quote Provided',
        '13. Revised FCOP Quote Provided')
  AND t.task_status IN ('pending', 'in_progress', 'submitted')
  AND t.task_assigned_to_name = 'Accounting';

-- Candidates: worklist LEFT JOIN priced lines (FA# primary, name fallback),
-- one row per matched priced line. match_status = 'priced' (matched a priced
-- invoice line => sendable) or 'unmatched' (no priced line). The finer
-- priced / matched_no_price / no_match diagnostic is a separate ad-hoc query
-- (it needs a full QP/PO scan; kept out of the view so the Phase-1 generator's
-- `WHERE match_status='priced'` stays fast). Name fallback matches the invoice
-- Site Name OR the newer-form "Site Name / Location Name".
CREATE OR REPLACE VIEW analytics.v_quote_candidates AS
SELECT
    w.task_did, w.asset_id, w.asset_name, w.task_name, w.task_status,
    w.fa_number, w.asset_name_norm,
    p.invoice_site_name, p.product_service, p.invoice_category, p.service_type,
    p.sow, p.service_rate, p.amount,
    CASE WHEN p.task_match IS NOT NULL THEN 'priced' ELSE 'unmatched' END AS match_status,
    CASE
        WHEN p.task_match IS NOT NULL AND w.fa_number IS NOT NULL AND p.match_fa = w.fa_number THEN 'fa'
        WHEN p.task_match IS NOT NULL THEN 'name'
        ELSE NULL
    END AS match_key
FROM analytics.v_quote_worklist w
LEFT JOIN LATERAL (
    SELECT i.site_name AS invoice_site_name, i.product_service, i.invoice_category,
           i.service_type, i.sow, i.service_rate, i.amount,
           i.fa_number AS match_fa, 1 AS task_match
    FROM analytics.v_quote_invoice_lines i
    WHERE (w.fa_number IS NOT NULL AND i.fa_number = w.fa_number)
       OR (w.asset_name_norm IS NOT NULL
           AND (i.site_name_norm = w.asset_name_norm OR i.location_name_norm = w.asset_name_norm))
) p ON TRUE;
