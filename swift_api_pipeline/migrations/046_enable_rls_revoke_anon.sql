-- migrations/046_enable_rls_revoke_anon.sql
-- Security fix: Enable RLS on all pipeline tables and revoke anon/authenticated access.
--
-- Context: Supabase security alert (April 19, 2026) flagged tables as publicly accessible.
-- All pipeline systems connect as postgres (service role) which bypasses RLS.
-- The anon grants were leftover from migrate_cloud.py and never used by any application.
--
-- Verified systems before applying:
--   local-pipeline (all pipelines)  → asyncpg as postgres     → NOT affected
--   GHA workflows (11 pipelines)    → asyncpg via pooler      → NOT affected
--   local-ai-agent (DARA)           → asyncpg as postgres     → NOT affected
--   pipeline-guardian               → asyncpg as postgres     → NOT affected
--   portal                          → not connected yet       → NOT affected
--   Apps Script triggers            → GitHub API only          → NOT affected
--
-- If something breaks after this migration, check if the failing system
-- connects via the anon key (Supabase REST API) instead of postgres.
-- Fix: either switch to service_role key or add an RLS policy for anon.
--
-- Applied: 2026-04-22

BEGIN;

-- ============================================================
-- 1. ENABLE ROW LEVEL SECURITY on all 58 pipeline tables
-- ============================================================

-- data_raw (19 tables)
ALTER TABLE data_raw.raw_asset_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_timer_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_daily_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_ar_aging ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_sales_detail ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_calendar_leave ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_user_priorities ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_timer_activities_historical ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_timer_discrepancies ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts13 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts14 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts15 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts16 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts17 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts18 ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_form_qa_ts19 ENABLE ROW LEVEL SECURITY;

-- data_staging (25 tables)
ALTER TABLE data_staging.stg_asset_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_activities_clean ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_corrections ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_entry_removals ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_duplicate_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_daily_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_timer_discrepancies ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_qa_form ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_ar_aging ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_sales_detail ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_user_priorities ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_daily_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_daily_report_hours ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_daily_report_attendance ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_calendar_leave ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_package_emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_fcop_status_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_cop_date_check_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.carrier_group_lookup ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.customer_name_lookup ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.qa_form_asset_did_lookup ENABLE ROW LEVEL SECURITY;

-- pipeline (3 tables)
ALTER TABLE pipeline.pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline.extraction_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline.cop_validator_migrations ENABLE ROW LEVEL SECURITY;

-- agent (10 tables)
ALTER TABLE agent.known_issues ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.monitor_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.permission_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.pipeline_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.role_permissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.schema_metadata ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.user_permission_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.users ENABLE ROW LEVEL SECURITY;

-- public (9 tables flagged by Supabase linter)
ALTER TABLE public.ingest_day_control ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ingest_error_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.roster ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_timerdata ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_timer_discrepancies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_qaform_ts15 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_qaform_ts16 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_qaform_ts17 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stg_qaform_ts18 ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- 2. REVOKE all anon + authenticated access from pipeline schemas
-- ============================================================

-- Revoke ALL privileges on tables
REVOKE ALL ON ALL TABLES IN SCHEMA data_raw FROM anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA data_staging FROM anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA pipeline FROM anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA agent FROM anon, authenticated;

-- Revoke ALL privileges on sequences (prevents serial/identity manipulation)
REVOKE ALL ON ALL SEQUENCES IN SCHEMA data_raw FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA data_staging FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA pipeline FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA agent FROM anon, authenticated;

-- Revoke ALL privileges on functions/RPCs in these schemas
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA data_raw FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA data_staging FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA pipeline FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agent FROM anon, authenticated;

-- Revoke USAGE on schemas (prevents even seeing what's inside)
REVOKE USAGE ON SCHEMA data_raw FROM anon, authenticated;
REVOKE USAGE ON SCHEMA data_staging FROM anon, authenticated;
REVOKE USAGE ON SCHEMA pipeline FROM anon, authenticated;
REVOKE USAGE ON SCHEMA agent FROM anon, authenticated;

-- ============================================================
-- 3. KEEP analytics SELECT for DARA (read-only, views only)
--    analytics views are read-only and intentionally exposed for querying.
--    But revoke any write access that shouldn't be there.
-- ============================================================
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA analytics FROM anon, authenticated;
-- SELECT on analytics views is intentionally kept for DARA agent queries.

-- ============================================================
-- 4. REVOKE anon access from reference tables
-- ============================================================
REVOKE ALL ON ALL TABLES IN SCHEMA reference FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA reference FROM anon, authenticated;
REVOKE USAGE ON SCHEMA reference FROM anon, authenticated;

-- ============================================================
-- 5. REVOKE anon access from the 9 public schema tables we manage
--    (other public tables managed by others are left untouched)
-- ============================================================
REVOKE ALL ON public.ingest_day_control FROM anon, authenticated;
REVOKE ALL ON public.ingest_error_log FROM anon, authenticated;
REVOKE ALL ON public.roster FROM anon, authenticated;
REVOKE ALL ON public.stg_timerdata FROM anon, authenticated;
REVOKE ALL ON public.stg_timer_discrepancies FROM anon, authenticated;
REVOKE ALL ON public.stg_qaform_ts15 FROM anon, authenticated;
REVOKE ALL ON public.stg_qaform_ts16 FROM anon, authenticated;
REVOKE ALL ON public.stg_qaform_ts17 FROM anon, authenticated;
REVOKE ALL ON public.stg_qaform_ts18 FROM anon, authenticated;

COMMIT;
