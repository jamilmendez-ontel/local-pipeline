-- 250_pmi_cluster_source_sheet.sql
-- Weekly PMI Report: let a market be sourced from a Google Sheet instead of the
-- pending + failing xlsx pair. Spec:
-- report-automation/docs/superpowers/specs/2026-08-28-wbv-sheet-intake-design.md
--
-- NULL keeps today's behaviour (xlsx only), so FL, CGC and MP are unaffected.
-- WBV's lead maintains "VZW WB VIR Pending/Failing PMI"; its tabs are named by the
-- day the list was circulated, NOT by Tracker Sent, so the reader takes the newest
-- tab by name and reads the week from the Tracker Sent column inside it.

ALTER TABLE reference.ref_pmi_clusters
    ADD COLUMN IF NOT EXISTS source_sheet_id text;

COMMENT ON COLUMN reference.ref_pmi_clusters.source_sheet_id IS
    'Google Sheets spreadsheet id for markets whose lead maintains a sheet. NULL = xlsx only.';

UPDATE reference.ref_pmi_clusters
   SET source_sheet_id = '1C12NGL1K3qCGWJpUnPkTLJcUw_zp3j1AXoPCVMYM1vU',
       updated_at      = now()
 WHERE market_code = 'WBV';
