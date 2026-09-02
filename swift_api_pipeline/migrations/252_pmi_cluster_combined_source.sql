-- 252_pmi_cluster_combined_source.sql
-- Weekly PMI Report: mark a market whose emailed workbook is ONE combined table
-- (pending + failing + completed mixed, source_kind derived per row) rather than
-- FL's pending/failing pair. Discovery record:
-- report-automation/weekly-pmi-report/reference/sample-input/2026-08-25/README.md
--
-- Before this column the combined gate keyed on source_sheet_id, so MP (combined
-- but emailed, no sheet) would load its whole file as 'pending' off the filename.
-- WBV is set true too: its combined-ness is intent, not a side effect of having a
-- sheet. FALSE keeps today's pair behaviour, so FL and CGC are unaffected.

ALTER TABLE reference.ref_pmi_clusters
    ADD COLUMN IF NOT EXISTS combined_source boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN reference.ref_pmi_clusters.combined_source IS
    'TRUE = the market sends ONE combined table per week (row kinds derived from '
    'status/notes). FALSE = FL-style pending/failing xlsx pair.';

UPDATE reference.ref_pmi_clusters
   SET combined_source = true,
       updated_at      = now()
 WHERE market_code IN ('MP', 'WBV');
