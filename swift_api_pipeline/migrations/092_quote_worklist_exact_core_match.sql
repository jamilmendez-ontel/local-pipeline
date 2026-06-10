-- 092_quote_worklist_exact_core_match.sql
--
-- Supersedes 091's contains-match. More precise + future-proof "Quote Provided"
-- detection: strip the leading number/punctuation prefix, collapse whitespace,
-- and require the CORE to be EXACTLY "Quote Provided".
--
--   "7. Quote Provided"               -> core "Quote Provided"               -> KEPT
--   "5. Quote Provided" / "10." / "7)"-> core "Quote Provided"               -> KEPT
--   "Quote Provided" (no number)      -> core "Quote Provided"               -> KEPT
--   "11. Revised FCOP Quote Provided" -> core "Revised FCOP Quote Provided"  -> dropped
--   "8. PO Issued"                    -> core "PO Issued"                    -> dropped
--   "Final Quote Provided - Resubmit" -> core "Final Quote Provided ..."     -> dropped
--
-- Only the LEADING prefix is stripped (^[^A-Za-z]*), NOT everything up to the
-- word "Quote" — otherwise "Revised FCOP Quote Provided" would collapse to
-- "Quote Provided" and wrongly match. So Revised FCOP / PO Issued fall out with
-- no separate exclusion clause, and a stray "...Quote Provided..." variant won't
-- be caught (which a plain ILIKE '%Quote Provided%' would have).
--
-- Verified live: worklist = 270 rows, 0 Revised FCOP, 0 PO Issued. Columns
-- unchanged; v_quote_worklist_enriched + the 3 quote MVs read FROM this view.
-- Refresh mv_quote_review, mv_quote_invoice_options, mv_quote_source_invoice_lines
-- after applying (nightly refresh_quote_mvs covers it). Perf: planner bitmap-scans
-- the Accounting partial index (089) on task_status then filters (~155ms live).

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
    AND t.task_assigned_to_name = 'Accounting'::text;
