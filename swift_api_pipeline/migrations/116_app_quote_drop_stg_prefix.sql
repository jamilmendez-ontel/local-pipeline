-- Migration 116: drop the stg_quote_ prefix on the 9 app_quote tables.
-- Spec: quote-automation/docs/superpowers/specs/2026-06-19-quote-app-table-rename-design.md
--
-- Pure naming cleanup. ALTER ... RENAME is metadata-only: rows, indexes, PKs, owned
-- sequences, RLS policies, and table ACLs (anon/authenticated REVOKEs + service_role
-- grants from migration 112) all travel with the table OID. The one view dependency
-- (analytics.v_quote_review -> overrides) is OID-bound and auto-rewrites its stored
-- definition. The claim_due_emails RPC references the queue table BY NAME under
-- search_path='', so it does NOT follow a rename and must be recreated.
--
-- COORDINATION (same as Phase C cutover): pause the every-1-min email-queue Apps Script
-- trigger first, run this, promote the app build referencing the new names, then
-- re-enable the trigger. Applied atomically by the migration runner (single transaction).
-- PREREQ: migrations 110/111/112 applied; Swift-branch merge has landed on main.

-- 1. Drop the by-name RPC (recreated below against the renamed table).
DROP FUNCTION IF EXISTS app_quote.claim_due_emails(integer);

-- 2. Rename the 9 tables.
ALTER TABLE app_quote.stg_quote_email_queue       RENAME TO email_queue;
ALTER TABLE app_quote.stg_quote_email_templates   RENAME TO email_templates;
ALTER TABLE app_quote.stg_quote_send_identities   RENAME TO send_identities;
ALTER TABLE app_quote.stg_quote_user_settings     RENAME TO user_settings;
ALTER TABLE app_quote.stg_quote_overrides         RENAME TO overrides;
ALTER TABLE app_quote.stg_quote_generated         RENAME TO generated;
ALTER TABLE app_quote.stg_quote_gmail_connections RENAME TO gmail_connections;
ALTER TABLE app_quote.stg_quote_swift_filings     RENAME TO swift_filings;
ALTER TABLE app_quote.stg_quote_swift_filing_log  RENAME TO swift_filing_log;

-- 3. Recreate the dispatcher claim RPC against the renamed table (verbatim logic from
--    migration 111, only the table name changed).
CREATE FUNCTION app_quote.claim_due_emails(p_limit integer DEFAULT 50)
RETURNS SETOF app_quote.email_queue
LANGUAGE sql
SET search_path = ''
AS $function$
  UPDATE app_quote.email_queue q
  SET claimed_at = now()
  WHERE q.id IN (
    SELECT id FROM app_quote.email_queue
    WHERE status = 'scheduled'
      AND returned_at IS NULL
      AND claimed_at IS NULL
      AND scheduled_at <= now()
    ORDER BY scheduled_at
    LIMIT p_limit
    FOR UPDATE SKIP LOCKED
  )
  RETURNING q.*;
$function$;

GRANT EXECUTE ON FUNCTION app_quote.claim_due_emails(integer) TO service_role;

-- 4. Update the governance catalog rows to the new names.
DELETE FROM agent.schema_metadata WHERE schema_name = 'app_quote';
INSERT INTO agent.schema_metadata (schema_name, table_name, description) VALUES
 ('app_quote','email_queue','Outbound quote-email queue: one row per scheduled/sent/failed/returned send. Claimed atomically by app_quote.claim_due_emails for the 1-min dispatcher.'),
 ('app_quote','email_templates','Saved quote-email templates (subject/body with {{tokens}}).'),
 ('app_quote','generated','Record of each generated quote PDF filed to Drive (task_did, drive_file_id, drive_link, generated_by/at).'),
 ('app_quote','gmail_connections','Per-user Gmail OAuth connections (encrypted refresh tokens) used to send quote emails.'),
 ('app_quote','user_settings','Per-user app settings: active sender/from, theme, connected Swift account (encrypted).'),
 ('app_quote','overrides','Per-task manual overrides: rate, product/service, line items, chosen line key.'),
 ('app_quote','send_identities','Shared + personal "send as" masks (aliases) for quote emails.'),
 ('app_quote','swift_filings','State of filing a sent quote back to Swift (one row per task_did).'),
 ('app_quote','swift_filing_log','Audit log of Swift write-back filing attempts (who/when/outcome).');
