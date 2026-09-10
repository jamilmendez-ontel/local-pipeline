-- 262_pmi_cluster_sheet_layout_cartn.sql
-- Weekly PMI Report: CAR-TN as the fifth market, sourced from Mary's TSC Construction
-- Tracker Google Sheet. Spec:
-- report-automation/docs/superpowers/specs/2026-09-10-car-tn-pmi-market-design.md
--
-- sheet_layout says HOW a sheet/combined source is read. 'pmi_tracker' (default) is the
-- FL/WBV/MP/CGC shape (FUZE Project ID header, Tracker Sent column). 'tsc_construction'
-- is CAR-TN's construction tracker (Fuze ID header, MMDDYYYY tab names, status derived
-- from the PMI COP column). Existing rows are unaffected.
--
-- DEPLOY ORDER: apply this BEFORE merging report-automation feat/pmi-car-tn-market.
-- Loader.cluster_by_code selects sheet_layout, so code on main without the column breaks
-- every market's run.

ALTER TABLE reference.ref_pmi_clusters
    ADD COLUMN IF NOT EXISTS sheet_layout text NOT NULL DEFAULT 'pmi_tracker';

ALTER TABLE reference.ref_pmi_clusters
    DROP CONSTRAINT IF EXISTS ref_pmi_clusters_sheet_layout_check;
ALTER TABLE reference.ref_pmi_clusters
    ADD CONSTRAINT ref_pmi_clusters_sheet_layout_check
    CHECK (sheet_layout IN ('pmi_tracker', 'tsc_construction'));

COMMENT ON COLUMN reference.ref_pmi_clusters.sheet_layout IS
    'How a sheet/combined source is read: pmi_tracker = FUZE Project ID header + Tracker Sent column (FL/WBV/MP/CGC); tsc_construction = Fuze ID header, MMDDYYYY tab names, PMI status derived from the PMI COP column (CAR-TN).';

INSERT INTO reference.ref_pmi_clusters
    (market_code, cluster, subtitle, page_variant, show_completion_line, active,
     registered_by, source_sheet_id, combined_source, sheet_layout)
VALUES
    ('CARTN', 'Beta', 'VZW/CAR-TN - Embedded', 'standard', true, true, 'seed',
     '1APGVTwhS43TliUDDZi5iyK4v_2QEycim8TrM-VKLE_M', true, 'tsc_construction')
ON CONFLICT (market_code) DO UPDATE
   SET cluster = EXCLUDED.cluster, subtitle = EXCLUDED.subtitle,
       source_sheet_id = EXCLUDED.source_sheet_id, combined_source = EXCLUDED.combined_source,
       sheet_layout = EXCLUDED.sheet_layout, active = true, updated_at = now();

-- Verify:
--   SELECT market_code, cluster, sheet_layout, source_sheet_id, combined_source
--     FROM reference.ref_pmi_clusters ORDER BY market_code;
--   expect CARTN/Beta/tsc_construction and every other row pmi_tracker.
-- Rollback:
--   DELETE FROM data_staging.stg_pmi_tracker_sites WHERE cluster = 'Beta';
--   DELETE FROM data_raw.raw_pmi_tracker_rows WHERE file_id IN
--       (SELECT file_id FROM data_raw.raw_pmi_tracker_files WHERE market_code = 'CARTN');
--   DELETE FROM data_raw.raw_pmi_tracker_files WHERE market_code = 'CARTN';
--   DELETE FROM reference.ref_pmi_clusters WHERE market_code = 'CARTN';
--   ALTER TABLE reference.ref_pmi_clusters DROP COLUMN sheet_layout;
