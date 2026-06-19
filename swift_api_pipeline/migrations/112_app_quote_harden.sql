-- Migration 112: app_quote defense-in-depth hardening (DATABASE_ARCHITECTURE.md s5 baseline).
-- Safe because the app connects as service_role, which has rolbypassrls=true (verified) and
-- thus bypasses RLS entirely; anon/authenticated/authenticator do NOT bypass.
--
-- 1) RLS deny-all (RLS enabled, no policies) on all 9 app_quote tables. Second wall behind
--    the schema-USAGE restriction: even if anon/authenticated were ever mis-granted USAGE,
--    they would still read zero rows.
-- 2) Revoke the inert anon/authenticated table/sequence grants that traveled with the tables
--    during the SET SCHEMA move (they cannot be used today without schema USAGE, but cleaned
--    up so the grant surface matches intent).

ALTER TABLE app_quote.stg_quote_email_queue       ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_email_templates   ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_generated         ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_gmail_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_user_settings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_overrides         ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_send_identities   ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_swift_filings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_quote.stg_quote_swift_filing_log  ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES    IN SCHEMA app_quote FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA app_quote FROM anon, authenticated;
