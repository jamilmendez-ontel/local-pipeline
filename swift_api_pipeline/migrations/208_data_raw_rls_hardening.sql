-- 208: data_raw RLS hardening
-- Context (verified 2026-08-02): 12 data_raw tables had RLS disabled AND standing
-- SELECT grants to anon/authenticated, while data_raw sits in the PostgREST
-- exposed-schemas list. The only protection was the schema-level USAGE revoke —
-- one future GRANT USAGE away from exposing raw business data via the anon key.
-- This closes the other two layers: revoke the stray grants (and the default
-- privileges that keep re-creating them) and enable deny-all RLS (enabled, no
-- policies). Owner (postgres) and service_role bypass RLS, so the pipeline,
-- MCP, and apps are unaffected. Scope: data_raw only; data_staging and the
-- PostgREST exposed-schemas list to be reviewed separately.

-- 1. Close standing table grants
REVOKE SELECT ON ALL TABLES IN SCHEMA data_raw FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA data_raw REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA data_raw REVOKE ALL ON TABLES FROM anon, authenticated;

-- 2. Deny-all RLS backstop on the 12 flagged tables
ALTER TABLE data_raw.raw_asset_tasks         ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_default ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_gc      ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts13    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts14    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts15    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts16    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts17    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts18    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_asset_tasks_ts19    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_assets              ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_invoicing_form      ENABLE ROW LEVEL SECURITY;
