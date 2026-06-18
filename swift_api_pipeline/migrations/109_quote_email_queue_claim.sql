-- 109_quote_email_queue_claim.sql
--
-- Email-queue dispatcher concurrency fix.
--
-- The dispatcher (/api/email-queue/process) runs on a 1-minute Apps Script trigger,
-- but a single run may take up to 120s on a large batch, so two runs overlapped and
-- both SELECTed the same 'scheduled' rows. Each Gmail draft sends exactly once, but
-- the second run re-hit the already-consumed draft and got 404 (-> marked cancelled)
-- or 400 Precondition (-> marked failed), clobbering the correct 'sent' status. The
-- 2026-06-18 138-email batch showed ~115 rows mislabeled cancelled/failed though all
-- 138 actually delivered.
--
-- Fix: claim rows atomically before sending. `claimed_at` marks a row as taken; the
-- claim_due_emails() RPC stamps it under FOR UPDATE SKIP LOCKED, so concurrent runs
-- receive disjoint sets and can never grab the same draft. A reaper (in the route)
-- frees rows whose run died by nulling claimed_at once it is older than the max run
-- time. Reclaiming is safe: an already-sent draft is gone, so a re-send 404s rather
-- than double-delivering.

ALTER TABLE data_staging.stg_quote_email_queue
  ADD COLUMN IF NOT EXISTS claimed_at timestamptz;

CREATE OR REPLACE FUNCTION data_staging.claim_due_emails(p_limit int DEFAULT 50)
RETURNS SETOF data_staging.stg_quote_email_queue
LANGUAGE sql
AS $$
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
$$;

GRANT EXECUTE ON FUNCTION data_staging.claim_due_emails(int) TO service_role;

-- Let PostgREST pick up the new function immediately.
NOTIFY pgrst, 'reload schema';
