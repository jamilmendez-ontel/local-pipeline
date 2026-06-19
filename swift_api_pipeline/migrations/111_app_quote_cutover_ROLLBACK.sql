-- ROLLBACK for migration 111 (app_quote cutover).
-- Run this + redeploy the prior app build (the commit BEFORE e57e246, i.e. revert the
-- "point app DB references at app_quote schema" commit, or Vercel Instant Rollback to
-- the prior production deployment). Restores the pre-cutover state exactly.
--
-- Order: drop the relocated RPC, move the 9 tables back, restore the original
-- data_staging.claim_due_emails (migration 109 body, verbatim), drop catalog rows.
-- The PostgREST app_quote exposure (migration 110) is harmless to leave in place.

DROP FUNCTION IF EXISTS app_quote.claim_due_emails(integer);

ALTER TABLE app_quote.stg_quote_email_queue       SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_email_templates   SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_generated         SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_gmail_connections SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_user_settings     SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_overrides         SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_send_identities   SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_swift_filings     SET SCHEMA data_staging;
ALTER TABLE app_quote.stg_quote_swift_filing_log  SET SCHEMA data_staging;

CREATE FUNCTION data_staging.claim_due_emails(p_limit integer DEFAULT 50)
RETURNS SETOF data_staging.stg_quote_email_queue
LANGUAGE sql
AS $function$
  UPDATE data_staging.stg_quote_email_queue q
  SET claimed_at = now()
  WHERE q.id IN (
    SELECT id FROM data_staging.stg_quote_email_queue
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

GRANT EXECUTE ON FUNCTION data_staging.claim_due_emails(integer) TO service_role;

DELETE FROM agent.schema_metadata WHERE schema_name = 'app_quote';
