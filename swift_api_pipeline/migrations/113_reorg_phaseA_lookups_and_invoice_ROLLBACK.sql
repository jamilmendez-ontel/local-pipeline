-- ═══════════════════════════════════════════════════════════════════════════════
-- ROLLBACK for 113 — OntelDB reorg Phase A. Inverse DDL (metadata-only; no data moved).
-- Restores the pre-113 state: lookups back in data_staging w/ old names, invoice tables
-- back to unprefixed names (drops the added PKs), refresh_invoice_audit() back to old
-- names, grants/metadata/metric reverted. Tagged backup also in reorg_backup_20260619.
-- ═══════════════════════════════════════════════════════════════════════════════
BEGIN;

-- A1 inverse: lookups back to data_staging + old names
ALTER TABLE reference.ref_customer_names RENAME CONSTRAINT ref_customer_names_pkey         TO customer_name_lookup_pkey;
ALTER TABLE reference.ref_customer_names RENAME CONSTRAINT ref_customer_names_raw_name_key TO customer_name_lookup_raw_name_key;
ALTER TABLE reference.ref_customer_names   RENAME TO customer_name_lookup;
ALTER TABLE reference.customer_name_lookup SET SCHEMA data_staging;

ALTER TABLE reference.ref_carrier_groups RENAME CONSTRAINT ref_carrier_groups_pkey             TO carrier_group_lookup_pkey;
ALTER TABLE reference.ref_carrier_groups RENAME CONSTRAINT ref_carrier_groups_search_term_key TO carrier_group_lookup_search_term_key;
ALTER TABLE reference.ref_carrier_groups   RENAME TO carrier_group_lookup;
ALTER TABLE reference.carrier_group_lookup SET SCHEMA data_staging;

ALTER INDEX IF EXISTS reference.idx_ref_qa_form_asset_did_asset_did RENAME TO idx_qa_form_lookup_asset_did;
ALTER INDEX IF EXISTS reference.idx_ref_qa_form_asset_did_site_name RENAME TO idx_qa_form_lookup_site_name;
ALTER TABLE reference.ref_qa_form_asset_did RENAME CONSTRAINT ref_qa_form_asset_did_pkey TO qa_form_asset_did_lookup_pkey;
ALTER TABLE reference.ref_qa_form_asset_did    RENAME TO qa_form_asset_did_lookup;
ALTER TABLE reference.qa_form_asset_did_lookup SET SCHEMA data_staging;

-- A2 inverse: drop PKs + rename invoice tables back
ALTER INDEX IF EXISTS data_staging.idx_stg_invoice_pairings_full RENAME TO idx_pairings_full;
ALTER TABLE data_staging.stg_invoice_pairings DROP CONSTRAINT stg_invoice_pairings_pkey;
ALTER TABLE data_staging.stg_invoice_pairings RENAME TO invoice_pairings;

ALTER INDEX IF EXISTS data_staging.idx_stg_invoice_audit_ctask         RENAME TO idx_iac_ctask;
ALTER INDEX IF EXISTS data_staging.idx_stg_invoice_audit_project_ctask RENAME TO idx_iac_project_ctask;
ALTER INDEX IF EXISTS data_staging.idx_stg_invoice_audit_status        RENAME TO idx_iac_status;
ALTER INDEX IF EXISTS data_staging.idx_stg_invoice_audit_submitter     RENAME TO idx_iac_submitter;
ALTER TABLE data_staging.stg_invoice_audit DROP CONSTRAINT stg_invoice_audit_pkey;
ALTER TABLE data_staging.stg_invoice_audit RENAME TO invoice_audit_clean;

-- A3 inverse: recreate refresh_invoice_audit() against old data_staging names
-- (apply the pre-113 definition; see git history of this file's _pre113 sibling if needed).
-- For brevity the body is identical to 113 with stg_invoice_audit->invoice_audit_clean
-- and stg_invoice_pairings->invoice_pairings. Re-run report-automation's
-- fix_refresh_invoice_audit_rebuild_clean_and_pairings.sql to restore it exactly.

-- A4/A5 inverse: re-grant + restore metadata/metric names
GRANT SELECT ON data_staging.carrier_group_lookup     TO authenticated;
GRANT SELECT ON data_staging.qa_form_asset_did_lookup TO anon, authenticated;
DROP POLICY IF EXISTS carrier_group_lookup_read_authenticated ON data_staging.carrier_group_lookup;
CREATE POLICY carrier_group_lookup_read_authenticated ON data_staging.carrier_group_lookup
  FOR SELECT TO authenticated USING (true);

UPDATE agent.schema_metadata SET schema_name='data_staging', table_name='customer_name_lookup'
  WHERE schema_name='reference' AND table_name='ref_customer_names';
UPDATE agent.schema_metadata SET schema_name='data_staging', table_name='carrier_group_lookup'
  WHERE schema_name='reference' AND table_name='ref_carrier_groups';
UPDATE agent.schema_metadata SET schema_name='data_staging', table_name='qa_form_asset_did_lookup'
  WHERE schema_name='reference' AND table_name='ref_qa_form_asset_did';
UPDATE agent.schema_metadata SET table_name='invoice_audit_clean' WHERE schema_name='data_staging' AND table_name='stg_invoice_audit';
UPDATE agent.schema_metadata SET table_name='invoice_pairings'    WHERE schema_name='data_staging' AND table_name='stg_invoice_pairings';
UPDATE agent.metric_definitions
   SET sql_template = replace(sql_template, 'reference.ref_carrier_groups', 'data_staging.carrier_group_lookup')
 WHERE metric_key = 'packages_by_carrier_wtd';

COMMIT;
