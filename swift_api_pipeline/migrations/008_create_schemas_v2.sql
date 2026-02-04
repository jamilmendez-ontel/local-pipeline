-- migrations/008_create_schemas_v2.sql
-- Reorganize tables into schemas by data layer
-- Using data_raw and data_staging to avoid conflicts with existing schemas

-- ============================================================
-- CREATE SCHEMAS (postgres-owned)
-- ============================================================

CREATE SCHEMA IF NOT EXISTS data_raw;
CREATE SCHEMA IF NOT EXISTS data_staging;
-- reference and pipeline already exist and are postgres-owned

-- ============================================================
-- MOVE RAW TABLES to data_raw
-- ============================================================

ALTER TABLE public.raw_organizations SET SCHEMA data_raw;
ALTER TABLE public.raw_projects SET SCHEMA data_raw;
ALTER TABLE public.raw_asset_tasks SET SCHEMA data_raw;
ALTER TABLE public.raw_user_priorities SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts13 SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts14 SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts15 SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts16 SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts17 SET SCHEMA data_raw;
ALTER TABLE public.raw_form_qa_ts18 SET SCHEMA data_raw;
ALTER TABLE public.raw_timer_activities SET SCHEMA data_raw;

-- ============================================================
-- MOVE STAGING TABLES to data_staging
-- ============================================================

-- Drop FK constraint before moving (will recreate after)
ALTER TABLE public.stg_timer_activities DROP CONSTRAINT IF EXISTS fk_timer_project;

ALTER TABLE public.stg_organizations SET SCHEMA data_staging;
ALTER TABLE public.stg_projects SET SCHEMA data_staging;
ALTER TABLE public.stg_asset_tasks SET SCHEMA data_staging;
ALTER TABLE public.stg_user_priorities SET SCHEMA data_staging;
ALTER TABLE public.stg_qa_form SET SCHEMA data_staging;
ALTER TABLE public.stg_timer_activities SET SCHEMA data_staging;

-- Recreate FK constraint with schema-qualified reference
ALTER TABLE data_staging.stg_timer_activities
ADD CONSTRAINT fk_timer_project
FOREIGN KEY (project_did) REFERENCES data_staging.stg_projects(project_did);

-- ============================================================
-- GRANT USAGE ON SCHEMAS (for supabase access)
-- ============================================================

GRANT USAGE ON SCHEMA data_raw TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA data_staging TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA reference TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA pipeline TO anon, authenticated, service_role;

-- Grant select on all tables in each schema
GRANT SELECT ON ALL TABLES IN SCHEMA data_raw TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA data_staging TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA reference TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA pipeline TO anon, authenticated;

-- Service role gets full access
GRANT ALL ON ALL TABLES IN SCHEMA data_raw TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA data_staging TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA reference TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA pipeline TO service_role;

-- Grant on sequences too
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA data_raw TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA data_staging TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA reference TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA pipeline TO service_role;

-- Default privileges for future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA data_raw GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA data_raw GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA data_staging GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA data_staging GRANT ALL ON TABLES TO service_role;
