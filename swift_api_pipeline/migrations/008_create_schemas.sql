-- migrations/008_create_schemas.sql
-- Reorganize tables into schemas by data layer

-- ============================================================
-- CREATE SCHEMAS
-- ============================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS pipeline;

-- ============================================================
-- MOVE RAW TABLES
-- ============================================================

ALTER TABLE public.raw_organizations SET SCHEMA raw;
ALTER TABLE public.raw_projects SET SCHEMA raw;
ALTER TABLE public.raw_asset_tasks SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts13 SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts14 SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts15 SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts16 SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts17 SET SCHEMA raw;
ALTER TABLE public.raw_form_qa_ts18 SET SCHEMA raw;
ALTER TABLE public.raw_timer_activities SET SCHEMA raw;

-- ============================================================
-- MOVE STAGING TABLES
-- ============================================================

-- Drop FK constraint before moving (will recreate after)
ALTER TABLE public.stg_timer_activities DROP CONSTRAINT IF EXISTS fk_timer_project;

ALTER TABLE public.stg_organizations SET SCHEMA staging;
ALTER TABLE public.stg_projects SET SCHEMA staging;
ALTER TABLE public.stg_asset_tasks SET SCHEMA staging;
ALTER TABLE public.stg_qa_form SET SCHEMA staging;
ALTER TABLE public.stg_timer_activities SET SCHEMA staging;

-- Recreate FK constraint with schema-qualified reference
ALTER TABLE staging.stg_timer_activities
ADD CONSTRAINT fk_timer_project
FOREIGN KEY (project_did) REFERENCES staging.stg_projects(project_did);

-- ============================================================
-- MOVE REFERENCE TABLES
-- ============================================================

ALTER TABLE public.ref_ontel_techops_projects SET SCHEMA reference;

-- ============================================================
-- MOVE PIPELINE TABLES
-- ============================================================

ALTER TABLE public.pipeline_runs SET SCHEMA pipeline;

-- ============================================================
-- GRANT USAGE ON SCHEMAS (for supabase access)
-- ============================================================

GRANT USAGE ON SCHEMA raw TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA staging TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA reference TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA pipeline TO anon, authenticated, service_role;

-- Grant select on all tables in each schema
GRANT SELECT ON ALL TABLES IN SCHEMA raw TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA staging TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA reference TO anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA pipeline TO anon, authenticated;

-- Service role gets full access
GRANT ALL ON ALL TABLES IN SCHEMA raw TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA staging TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA reference TO service_role;
GRANT ALL ON ALL TABLES IN SCHEMA pipeline TO service_role;

-- Grant on sequences too
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA raw TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA staging TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA reference TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA pipeline TO service_role;
