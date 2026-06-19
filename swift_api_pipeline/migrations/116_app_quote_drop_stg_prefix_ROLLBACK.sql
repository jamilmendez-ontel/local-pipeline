-- Rollback for migration 116: restore the stg_quote_ names.
-- Inverse of every statement in 116, in reverse dependency order.

DROP FUNCTION IF EXISTS app_quote.claim_due_emails(integer);

ALTER TABLE app_quote.email_queue       RENAME TO stg_quote_email_queue;
ALTER TABLE app_quote.email_templates   RENAME TO stg_quote_email_templates;
ALTER TABLE app_quote.send_identities   RENAME TO stg_quote_send_identities;
ALTER TABLE app_quote.user_settings     RENAME TO stg_quote_user_settings;
ALTER TABLE app_quote.overrides         RENAME TO stg_quote_overrides;
ALTER TABLE app_quote.generated         RENAME TO stg_quote_generated;
ALTER TABLE app_quote.gmail_connections RENAME TO stg_quote_gmail_connections;
ALTER TABLE app_quote.swift_filings     RENAME TO stg_quote_swift_filings;
ALTER TABLE app_quote.swift_filing_log  RENAME TO stg_quote_swift_filing_log;

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
