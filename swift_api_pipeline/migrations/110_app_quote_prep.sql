-- Migration 110: app_quote schema prep (Phase C of the OntelDB reorg)
-- Companion: ai-projects/DATABASE_REORG_PLAN.md (Phase C, step C0).
--
-- Creates the dedicated live-OLTP schema for Quote Automation and exposes it to
-- the app the same way data_staging is reached today. NO data moves here and the
-- live app is unaffected (it still references data_staging until the cutover
-- migration 111 runs in lockstep with the app redeploy). Zero downtime.
--
-- Access model mirrors the current quote tables: the app connects ONLY as
-- service_role (createServiceClient); anon/authenticated never get USAGE.

CREATE SCHEMA IF NOT EXISTS app_quote;

-- The app's role. service_role bypasses RLS; it is the only role that reaches
-- these tables today (data_staging USAGE is granted only to postgres + service_role).
GRANT USAGE ON SCHEMA app_quote TO service_role;

-- Future objects created in app_quote by postgres are reachable by service_role.
ALTER DEFAULT PRIVILEGES IN SCHEMA app_quote GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA app_quote GRANT ALL ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA app_quote GRANT EXECUTE ON FUNCTIONS TO service_role;

-- Per-table catalog rows in agent.schema_metadata are added by the cutover
-- migration (111), once the tables physically live in app_quote.
