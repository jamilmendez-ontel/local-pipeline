-- 091_quote_worklist_prefix_agnostic.sql
--
-- Accounting confirmed the quote worklist is "Quote Provided" only, but the
-- leading number ("7.") is not stable: other orgs / future Swift workflows can
-- label it with a different number. The old v_quote_worklist hardcoded
-- task_name = ANY('7. Quote Provided','8. PO Issued','11/12/13. Revised FCOP
-- Quote Provided'), which would silently drop a "5. Quote Provided" etc.
--
-- Replace the exact-list filter with a prefix-agnostic match that honors the
-- locked decisions:
--   * task_name ILIKE '%Quote Provided%'      -> any (or no) leading number
--   * AND task_name NOT ILIKE '%Revised FCOP%' -> Revised FCOP excluded (#3)
--   * PO Issued drops out naturally (no "Quote Provided" in the name) (#4)
--   * task_status IN (pending,in_progress,submitted), assigned = 'Accounting'
--
-- v_quote_worklist_enriched and the quote MVs read FROM this view (no task_name
-- filter of their own), so this one change fixes every path. Columns unchanged,
-- so dependents are structurally unaffected. Current count is identical (270 —
-- only "7." exists in the data today); the change is future-proofing.
--
-- The partial index idx_stg_asset_tasks_quote_worklist (task_name, task_status)
-- WHERE assigned='Accounting' (migration 089) still serves this: the planner
-- bitmap-scans on task_status within the Accounting partial index and filters
-- the ILIKE (~155ms live; the Queue reads the materialized mv_quote_review).
--
-- After applying: refresh mv_quote_review, mv_quote_invoice_options,
-- mv_quote_source_invoice_lines (the nightly refresh_quote_mvs does this too).

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
  WHERE t.task_name ILIKE '%Quote Provided%'
    AND t.task_name NOT ILIKE '%Revised FCOP%'
    AND (t.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'submitted'::text]))
    AND t.task_assigned_to_name = 'Accounting'::text;
