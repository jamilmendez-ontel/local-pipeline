-- ═══════════════════════════════════════════════════════════════════════════════
-- 113 — OntelDB reorg Phase A: lookups -> reference (ref_*), invoice tables -> stg_* + PKs
-- ═══════════════════════════════════════════════════════════════════════════════
-- Companion: ai-projects/DATABASE_REORG_PLAN.md (Phase A) + DATABASE_ARCHITECTURE.md.
--
-- WHAT THIS DOES
--   A1. Move the 3 lookup tables out of data_staging into reference, with ref_ prefix:
--         customer_name_lookup     -> reference.ref_customer_names
--         carrier_group_lookup     -> reference.ref_carrier_groups
--         qa_form_asset_did_lookup -> reference.ref_qa_form_asset_did
--   A2. Rename the 2 unprefixed warehouse tables (stay in data_staging) and add PKs on
--       their verified-unique natural keys:
--         invoice_audit_clean -> stg_invoice_audit      PK (task_did)            [613,116 rows, 0 dup, 0 null]
--         invoice_pairings    -> stg_invoice_pairings   PK (project_did, asset_did, paired_complete_ctask) [306,558 rows, 0 dup, 0 null]
--   A3. Recreate analytics.refresh_invoice_audit() (plpgsql, references the tables by
--       name) pointed at the new stg_ names. Body otherwise verbatim as of 2026-06-19.
--   A4. Security baseline (§5): the moved lookups get the clean reference posture —
--       service_role only; revoke anon/authenticated (this also FIXES the pre-existing
--       qa_form_asset_did_lookup anon/authenticated grant from migration 019). RLS stays
--       enabled (deny-all for non-service; service_role bypasses). The vestigial DARA
--       authenticated read policy on carrier is dropped (already non-functional: reference
--       schema USAGE is revoked from authenticated, so it cannot reach the schema).
--   A5. Update the catalog: agent.schema_metadata rows + the DARA metric_definitions
--       sql_template (packages_by_carrier_wtd) to the new reference.ref_carrier_groups name.
--
-- NOTE ON DARA: agent.execute_user_query is SECURITY INVOKER; DARA's documented intent is
--   to run as the authenticated user. authenticated already lacks USAGE on data_staging AND
--   reference, so this move is access-NEUTRAL (no regression). The metric works whenever it
--   is executed by service_role; the authenticated path was already blocked by the 2026-06-17
--   hardening. Re-granting authenticated into reference is intentionally NOT done (would
--   re-expose 8 dormant-granted reference tables incl. PII). See WORK_LOG for the full note.
--
-- Tagged backup taken first into schema reorg_backup_20260619 (pg_dump/CLI absent on host).
-- Rollback: 113_reorg_phaseA_lookups_and_invoice_ROLLBACK.sql
-- These tables are not written row-by-row by any live web app; a brief ACCESS EXCLUSIVE
-- lock during the rename/move is harmless. Verified no pipeline run in-flight before apply.
-- ═══════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ───────────────────────────── A1: lookups -> reference ─────────────────────────
ALTER TABLE data_staging.customer_name_lookup     SET SCHEMA reference;
ALTER TABLE reference.customer_name_lookup         RENAME TO ref_customer_names;
ALTER TABLE reference.ref_customer_names RENAME CONSTRAINT customer_name_lookup_pkey          TO ref_customer_names_pkey;
ALTER TABLE reference.ref_customer_names RENAME CONSTRAINT customer_name_lookup_raw_name_key  TO ref_customer_names_raw_name_key;

ALTER TABLE data_staging.carrier_group_lookup      SET SCHEMA reference;
ALTER TABLE reference.carrier_group_lookup          RENAME TO ref_carrier_groups;
ALTER TABLE reference.ref_carrier_groups RENAME CONSTRAINT carrier_group_lookup_pkey            TO ref_carrier_groups_pkey;
ALTER TABLE reference.ref_carrier_groups RENAME CONSTRAINT carrier_group_lookup_search_term_key TO ref_carrier_groups_search_term_key;

ALTER TABLE data_staging.qa_form_asset_did_lookup  SET SCHEMA reference;
ALTER TABLE reference.qa_form_asset_did_lookup      RENAME TO ref_qa_form_asset_did;
ALTER TABLE reference.ref_qa_form_asset_did RENAME CONSTRAINT qa_form_asset_did_lookup_pkey TO ref_qa_form_asset_did_pkey;
ALTER INDEX IF EXISTS reference.idx_qa_form_lookup_asset_did RENAME TO idx_ref_qa_form_asset_did_asset_did;
ALTER INDEX IF EXISTS reference.idx_qa_form_lookup_site_name RENAME TO idx_ref_qa_form_asset_did_site_name;

-- ───────────────────────── A2: invoice tables -> stg_ + PK ──────────────────────
ALTER TABLE data_staging.invoice_audit_clean RENAME TO stg_invoice_audit;
ALTER TABLE data_staging.stg_invoice_audit
  ADD CONSTRAINT stg_invoice_audit_pkey PRIMARY KEY (task_did);
ALTER INDEX IF EXISTS data_staging.idx_iac_ctask         RENAME TO idx_stg_invoice_audit_ctask;
ALTER INDEX IF EXISTS data_staging.idx_iac_project_ctask RENAME TO idx_stg_invoice_audit_project_ctask;
ALTER INDEX IF EXISTS data_staging.idx_iac_status        RENAME TO idx_stg_invoice_audit_status;
ALTER INDEX IF EXISTS data_staging.idx_iac_submitter     RENAME TO idx_stg_invoice_audit_submitter;

ALTER TABLE data_staging.invoice_pairings RENAME TO stg_invoice_pairings;
ALTER TABLE data_staging.stg_invoice_pairings
  ADD CONSTRAINT stg_invoice_pairings_pkey PRIMARY KEY (project_did, asset_did, paired_complete_ctask);
ALTER INDEX IF EXISTS data_staging.idx_pairings_full RENAME TO idx_stg_invoice_pairings_full;

-- ──────────────── A3: recreate refresh_invoice_audit() with new names ───────────
CREATE OR REPLACE FUNCTION analytics.refresh_invoice_audit()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  v_synced     integer;
  v_clean_rows integer;
  v_pair_rows  integer;
  v_rows       integer;
BEGIN
  -- 1) auto-detect new TS projects
  v_synced := reference.sync_tracked_projects();
  RAISE NOTICE 'refresh_invoice_audit: synced % new project(s)', v_synced;

  -- 2) Rebuild stg_invoice_audit from current v_asset_tasks_cleaned
  TRUNCATE data_staging.stg_invoice_audit;
  INSERT INTO data_staging.stg_invoice_audit (
    project_did, asset_did, asset_id, asset_name, project_status,
    task_did, task_name, ctask, task_status,
    task_submitted_on, task_approved_on,
    task_assigned_to_name, task_submitted_by_name
  )
  SELECT
    project_did, asset_did, asset_id, asset_name, project_status,
    task_did, task_name, cleaned_task_name, task_status,
    task_submitted_on, task_approved_on,
    task_assigned_to_name, task_submitted_by_name
  FROM data_staging.v_asset_tasks_cleaned;
  GET DIAGNOSTICS v_clean_rows = ROW_COUNT;
  RAISE NOTICE 'refresh_invoice_audit: rebuilt stg_invoice_audit (% rows)', v_clean_rows;

  -- 3) Rebuild stg_invoice_pairings from the fresh clean table
  TRUNCATE data_staging.stg_invoice_pairings;
  INSERT INTO data_staging.stg_invoice_pairings (
    project_did, asset_did, paired_complete_ctask, invoice_status, invoice_assigned_to
  )
  SELECT
    project_did,
    asset_did,
    REPLACE(ctask, 'Invoiced', 'Complete'),
    task_status,
    task_assigned_to_name
  FROM data_staging.stg_invoice_audit
  WHERE ctask ILIKE '%invoiced%';
  GET DIAGNOSTICS v_pair_rows = ROW_COUNT;
  RAISE NOTICE 'refresh_invoice_audit: rebuilt stg_invoice_pairings (% rows)', v_pair_rows;

  -- 4) Clear invoice_audit and rebuild (existing logic, unchanged)
  TRUNCATE analytics.invoice_audit;

  WITH approved_complete AS (
    SELECT c.*
    FROM data_staging.stg_invoice_audit c
    INNER JOIN reference.ref_invoice_audit_projects p
      ON p.project_did = c.project_did AND p.is_tracked = TRUE
    WHERE c.task_status = 'approved'
      AND c.ctask NOT ILIKE '%invoiced%'
  ),
  paired AS (
    SELECT c.*,
           inv.invoice_status      AS c_invoice_status,
           inv.invoice_assigned_to AS c_invoice_assigned_to
    FROM approved_complete c
    LEFT JOIN data_staging.stg_invoice_pairings inv
      ON  inv.project_did           = c.project_did
      AND inv.asset_did             = c.asset_did
      AND inv.paired_complete_ctask = c.ctask
  )
  INSERT INTO analytics.invoice_audit (
    project_did, asset_did, asset_id, asset_name, project_status,
    task_did, task_name, ctask, task_status,
    task_submitted_on, task_approved_on,
    task_submitted_by_name, task_assigned_to_name,
    c_invoice_status, c_invoice_assigned_to,
    c_beyond_48hrs, c_denom, c_num
  )
  SELECT
    project_did, asset_did, asset_id, asset_name, project_status,
    task_did, task_name, ctask, task_status,
    task_submitted_on, task_approved_on,
    task_submitted_by_name, task_assigned_to_name,
    c_invoice_status, c_invoice_assigned_to,
    CASE
      WHEN task_submitted_on IS NOT NULL
           AND (CURRENT_DATE - task_submitted_on::date) <= 1
      THEN 0 ELSE 1
    END,
    1,
    CASE
      WHEN ctask = 'Final COP Complete' AND c_invoice_status IN ('approved','cancelled') THEN 1
      WHEN c_invoice_status = 'cancelled' THEN 1
      WHEN c_invoice_status IN ('approved','submitted') THEN 1
      WHEN c_invoice_status IN ('pending','in_progress')
           AND task_submitted_by_name IN ('Myka Florano','Kyla Palo','Roy Riotoc')
        THEN 1
      ELSE 0
    END
  FROM paired;

  GET DIAGNOSTICS v_rows = ROW_COUNT;

  -- 5) Populate cluster (carrier group) — DAX-translated CASE
  UPDATE analytics.invoice_audit SET cluster = (
    CASE
      WHEN asset_id LIKE '%NIS/GA-AL%' OR asset_id LIKE '%NIS/PrimeCore Technologies/Oct 2021%' THEN 'Verizon'
      WHEN (asset_id LIKE '%VZW%' AND asset_id LIKE '%BAWA%') OR (asset_id LIKE '%South%' AND asset_id LIKE '%BAWA%') THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%CAR-TN%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%FL%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%GA-AL%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%KS-MO%' THEN 'Verizon'
      WHEN asset_id LIKE '%TSC/Sprint/GA%' THEN 'Beta'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%BAWA/Decom%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%BAWA%' THEN 'TMO/USCC'
      WHEN (asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%TMO/USCC%') OR (asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%CAR%') THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%Direct%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%FL%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/STX%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%GA%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/PA%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%SOVA%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/VA-Overlay%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/WV-Overlay%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%UPNY%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Volta%' AND asset_id LIKE '%2021%' THEN 'Epsilon'
      WHEN asset_id LIKE '%Volta%' THEN 'Delta'
      WHEN asset_id LIKE '%VZW/AR%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%CTX%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%DAL%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%Tri-State%' THEN 'Verizon'
      WHEN asset_id ILIKE '%Dish Wireless%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%T3 Wireless%' THEN 'Epsilon'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%RMR%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%CGC%' THEN 'Verizon'
      WHEN asset_id LIKE '%Finish Tower/Tower Inspections%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Motorola%' AND asset_id LIKE '%SOVA%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%SOCAL%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%/MP%' THEN 'Verizon'
      WHEN asset_id LIKE '%NB+C SE/SOVA/Small Cell/15453915/Jul 2021%' OR asset_id LIKE '%NB+C SE/T-Mobile/Anchor/Dec 2020%' THEN 'Verizon'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%MS-AL%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Finish Tower/US Cellular%' OR asset_id LIKE '%US Cellular%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%HGC%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%SOVA%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%NYM%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%OPW%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%PNW%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%Great Plains%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%UPNY%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%NORCAL%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%NYW%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%Mountain Plain%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW%' AND asset_id LIKE '%New England%' THEN 'Verizon'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%NYM%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%NJ%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%SOCAL%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/VA%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Gulf Services%' OR asset_id LIKE '%GulfServices%' THEN 'Gamma'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%BAWA%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%MI-IN%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%NORCAL%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%AR-OK%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%/VA%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%AT&T%' AND asset_id LIKE '%/OH%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/Louisiana%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile%' AND asset_id LIKE '%/Alabama%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%VZW/Southwest%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW/MI-IN-KY%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW/DECOM%' THEN 'Verizon'
      WHEN asset_id LIKE '%T-Mobile/WV%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%T-Mobile/NE%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%VZW/NE%' THEN 'Verizon'
      WHEN asset_id LIKE '%VZW/AAHI%' THEN 'Verizon'
      WHEN asset_id LIKE '%ByVerTek/FL/FTTH%' OR asset_id LIKE '%FL/FTTH%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%AT&T/%' THEN 'AT&T/DISH'
      WHEN asset_id LIKE '%Westell/CGC%' THEN 'Verizon'
      WHEN asset_id LIKE '%CGC/Westell%' THEN 'Verizon'
      WHEN asset_id LIKE '%Viaero/CO-NE%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%TSC%' THEN 'Beta'
      WHEN asset_id LIKE '%TCE%' THEN 'Alpha'
      WHEN asset_id LIKE '%Spectra Services/4 - Bay%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Spectra Services/Fiber Hut%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%Spectra Services/Amazon Kuiper%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%MO11&12/FTTH%' THEN 'TMO/USCC'
      WHEN asset_id LIKE '%MO9&10/FTTH%' THEN 'TMO/USCC'
      ELSE 'Unclassified'
    END
  );

  RAISE NOTICE 'refresh_invoice_audit: inserted % rows', v_rows;
  RETURN v_rows;
END;
$function$;

-- ───────────────── A4: security baseline on the moved lookups (§5) ──────────────
DROP POLICY IF EXISTS carrier_group_lookup_read_authenticated ON reference.ref_carrier_groups;
REVOKE ALL ON reference.ref_customer_names    FROM anon, authenticated;
REVOKE ALL ON reference.ref_carrier_groups    FROM anon, authenticated;
REVOKE ALL ON reference.ref_qa_form_asset_did FROM anon, authenticated;  -- fixes mig 019 anon/auth grant
GRANT SELECT, INSERT, UPDATE, DELETE ON reference.ref_customer_names    TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON reference.ref_carrier_groups    TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON reference.ref_qa_form_asset_did TO service_role;

-- ───────────────── A5: catalog updates (schema_metadata + DARA metric) ──────────
UPDATE agent.schema_metadata SET schema_name='reference', table_name='ref_customer_names',   updated_at=now()
  WHERE schema_name='data_staging' AND table_name='customer_name_lookup';
UPDATE agent.schema_metadata SET schema_name='reference', table_name='ref_carrier_groups',    updated_at=now()
  WHERE schema_name='data_staging' AND table_name='carrier_group_lookup';
UPDATE agent.schema_metadata SET schema_name='reference', table_name='ref_qa_form_asset_did', updated_at=now()
  WHERE schema_name='data_staging' AND table_name='qa_form_asset_did_lookup';
UPDATE agent.schema_metadata SET table_name='stg_invoice_audit',    updated_at=now()
  WHERE schema_name='data_staging' AND table_name='invoice_audit_clean';
UPDATE agent.schema_metadata SET table_name='stg_invoice_pairings', updated_at=now()
  WHERE schema_name='data_staging' AND table_name='invoice_pairings';

UPDATE agent.metric_definitions
   SET sql_template = replace(sql_template, 'data_staging.carrier_group_lookup', 'reference.ref_carrier_groups'),
       updated_at   = now()
 WHERE metric_key = 'packages_by_carrier_wtd';

COMMIT;
