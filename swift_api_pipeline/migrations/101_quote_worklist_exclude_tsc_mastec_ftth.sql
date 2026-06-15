-- 101_quote_worklist_exclude_tsc_mastec_ftth.sql
--
-- Exclude assets that should not be quoted through this tool, identified by tokens
-- in the asset_id Site-ID path:
--   * any path containing BOTH 'MasTec' and 'FTTH'  (MasTec FTTH build work)
--   * any path containing 'TSC'                     (TSC Construction / TSC subcon)
-- A MasTec path that is NOT FTTH (e.g. MasTec/VZW/CGC/Embedded/...) is KEPT.
--
-- Only the WHERE clause changes from migration 092; the SELECT list is byte-for-byte
-- identical (CREATE OR REPLACE VIEW cannot drop/reorder columns). v_quote_worklist_enriched,
-- v_quote_candidates, and the 3 quote MVs read FROM this view. Refresh
-- mv_quote_review, mv_quote_invoice_options, mv_quote_source_invoice_lines after applying.
--
-- Live measure 2026-06-15: worklist 248 -> 208 (40 excluded: 37 MasTec+FTTH, 3 TSC;
-- 20 MasTec-non-FTTH rows retained).

CREATE OR REPLACE VIEW analytics.v_quote_worklist AS
 SELECT t.task_did,
    t.project_did,
    t.asset_id,
    t.asset_name,
    t.task_name,
    t.task_status,
    t.task_assigned_to_name,
    ( SELECT mm.mm[1] AS mm
           FROM regexp_matches(COALESCE(t.asset_id, ''::text), '(\d{6,9})'::text, 'g'::text) mm(mm)
          ORDER BY (length(mm.mm[1])) DESC, (mm.mm[1])
         LIMIT 1) AS fa_number,
    NULLIF(upper(regexp_replace(TRIM(BOTH FROM COALESCE(t.asset_name, ''::text)), '\s+'::text, ' '::text, 'g'::text)), ''::text) AS asset_name_norm
   FROM data_staging.stg_asset_tasks t
  WHERE upper(btrim(regexp_replace(regexp_replace(t.task_name, '^[^A-Za-z]*'::text, ''::text), '\s+'::text, ' '::text, 'g'::text))) = 'QUOTE PROVIDED'::text
    AND (t.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'submitted'::text]))
    AND t.task_assigned_to_name = 'Accounting'::text
    AND NOT (
      (t.asset_id ILIKE '%MasTec%' AND t.asset_id ILIKE '%FTTH%')
      OR t.asset_id ILIKE '%TSC%'
    );
