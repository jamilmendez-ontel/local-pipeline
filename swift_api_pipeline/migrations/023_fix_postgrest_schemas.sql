-- Migration 023: Fix PostgREST schemas + create ref_ontel_techops_projects on cloud
--
-- Two issues fixed:
-- 1. Migration 021 excluded 'reference' from pgrst.db_schemas — broke 3 extractors
-- 2. ref_ontel_techops_projects table was never migrated to cloud (omitted from migrate_data_to_cloud.py)
--
-- Solution: Add 'reference' back to PostgREST, then create ref_ontel_techops_projects
-- as a VIEW on stg_projects so it stays in sync automatically after each pipeline run.

-- Fix PostgREST allowed schemas
ALTER ROLE authenticator SET pgrst.db_schemas = 'public, data_raw, data_staging, pipeline, agent, analytics, reference';
NOTIFY pgrst, 'reload config';

-- Ensure reference schema has proper grants
GRANT USAGE ON SCHEMA reference TO anon, authenticated, service_role;
GRANT SELECT ON ALL TABLES IN SCHEMA reference TO anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA reference TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA reference GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA reference GRANT ALL ON TABLES TO service_role;

-- Create ref_ontel_techops_projects as a VIEW (derives project_number from project name)
CREATE OR REPLACE VIEW reference.ref_ontel_techops_projects AS
SELECT
    project_did,
    project_name,
    org_did,
    org_name,
    status,
    asset_task_count,
    asset_task_pending,
    asset_task_approved,
    asset_task_in_progress,
    asset_project_count,
    date_created,
    last_updated,
    CAST(REGEXP_REPLACE(project_name, '^TECH-OPS: TS', '') AS INTEGER) AS project_number
FROM data_staging.stg_projects
WHERE project_name ~ '^TECH-OPS: TS\d+$'
  AND org_name = 'Ontel';

-- Grant access on the new view
GRANT SELECT ON reference.ref_ontel_techops_projects TO anon, authenticated;
GRANT ALL ON reference.ref_ontel_techops_projects TO service_role;
