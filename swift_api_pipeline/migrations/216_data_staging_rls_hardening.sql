-- 216: data_staging RLS hardening (follow-up to 208, which scoped itself to data_raw)
-- Context (verified 2026-08-03): 9 data_staging tables had RLS disabled AND standing
-- grants (SELECT/INSERT/UPDATE/DELETE/TRUNCATE) to anon/authenticated. Not actually
-- reachable today: schema USAGE was revoked from anon/authenticated in 046, and
-- pg_stat_statements shows zero anon/authenticated statements ever touching
-- data_staging (anon = 48 calls, all realtime/introspection internals). This closes
-- the remaining layers the same way 208 did for data_raw: revoke the dead grants
-- (and the default privileges that keep re-creating them on new tables) and enable
-- deny-all RLS (enabled, no policies). Owner (postgres) and service_role bypass
-- RLS, so the pipeline, MCP, and apps are unaffected.

-- 1. Close standing table grants
REVOKE ALL ON ALL TABLES IN SCHEMA data_staging FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA data_staging REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA data_staging REVOKE ALL ON TABLES FROM anon, authenticated;

-- 2. Deny-all RLS backstop on the 9 flagged tables
ALTER TABLE data_staging.stg_asset_tasks_gc             ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_assets_gc                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_targeted_asset_tasks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_targeted_task_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_invoicing_form             ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_invoice_pairings           ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_invoice_audit              ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_calendar_events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_48hr_status_snapshot       ENABLE ROW LEVEL SECURITY;
