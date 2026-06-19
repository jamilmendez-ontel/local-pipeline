-- Migration 111: app_quote cutover (Phase C, step C1) — RUN IN LOCKSTEP WITH THE APP REDEPLOY.
-- Companion: ai-projects/DATABASE_REORG_PLAN.md. Prereq: migration 110 applied.
--
-- Moves the 9 live Quote Automation OLTP tables out of data_staging into app_quote,
-- and relocates the claim_due_emails dispatcher RPC (its body references the queue
-- table BY NAME, so unlike views it does not survive SET SCHEMA — it must be recreated).
--
-- ALTER ... SET SCHEMA is metadata-only: rows, indexes, PKs, owned sequences, and the
-- table-level ACLs all travel with the table. The lone view dependency
-- (analytics.v_quote_review -> stg_quote_overrides) is OID-bound and keeps working.
--
-- COORDINATION: pause the every-1-min email-queue Apps Script trigger first so no
-- dispatch run is mid-write, then run this, then promote the app build that references
-- app_quote (commit on main), then re-enable the trigger.
--
-- Applied atomically by the migration runner (single transaction).

ALTER TABLE data_staging.stg_quote_email_queue       SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_email_templates   SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_generated         SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_gmail_connections SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_user_settings     SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_overrides         SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_send_identities   SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_swift_filings     SET SCHEMA app_quote;
ALTER TABLE data_staging.stg_quote_swift_filing_log  SET SCHEMA app_quote;

-- Relocate the dispatcher claim RPC (migration 109) to app_quote. Same logic,
-- now schema-qualified to app_quote + search_path pinned per the governance baseline.
DROP FUNCTION IF EXISTS data_staging.claim_due_emails(integer);

CREATE FUNCTION app_quote.claim_due_emails(p_limit integer DEFAULT 50)
RETURNS SETOF app_quote.stg_quote_email_queue
LANGUAGE sql
SET search_path = ''
AS $function$
  UPDATE app_quote.stg_quote_email_queue q
  SET claimed_at = now()
  WHERE q.id IN (
    SELECT id FROM app_quote.stg_quote_email_queue
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

-- Per-table catalog rows (governance baseline). table_name + schema_name + description
-- are NOT NULL. Re-runnable: delete any prior app_quote rows first.
DELETE FROM agent.schema_metadata WHERE schema_name = 'app_quote';
INSERT INTO agent.schema_metadata (schema_name, table_name, description) VALUES
 ('app_quote','stg_quote_email_queue','Outbound quote-email queue: one row per scheduled/sent/failed/returned send. Claimed atomically by app_quote.claim_due_emails for the 1-min dispatcher.'),
 ('app_quote','stg_quote_email_templates','Saved quote-email templates (subject/body with {{tokens}}).'),
 ('app_quote','stg_quote_generated','Record of each generated quote PDF filed to Drive (task_did, drive_file_id, drive_link, generated_by/at).'),
 ('app_quote','stg_quote_gmail_connections','Per-user Gmail OAuth connections (encrypted refresh tokens) used to send quote emails.'),
 ('app_quote','stg_quote_user_settings','Per-user app settings: active sender/from, theme, connected Swift account (encrypted).'),
 ('app_quote','stg_quote_overrides','Per-task manual overrides: rate, product/service, line items, chosen line key.'),
 ('app_quote','stg_quote_send_identities','Shared + personal "send as" masks (aliases) for quote emails.'),
 ('app_quote','stg_quote_swift_filings','State of filing a sent quote back to Swift (one row per task_did).'),
 ('app_quote','stg_quote_swift_filing_log','Audit log of Swift write-back filing attempts (who/when/outcome).');
