-- 239: ops_alerted_at - completes the DB-backed alert queue
-- (pre-merge resilience review, 2026-08-20)
--
-- Findings P1/P2/CR1/CR2: every email obligation lived only in run-local
-- memory while the DB state gating it was already committed, so a failed
-- send (Gmail outage, killed run) was dropped forever. The fix makes the
-- registry row the queue: ops_alerted_at / notice_sent_at /
-- resolved_notice_sent_at are stamped only on successful send and reset by
-- the upsert when an anomaly re-opens or re-breaks, so unsent alerts retry
-- on every subsequent run.

ALTER TABLE pipeline.schedule_audit_anomalies
  ADD COLUMN IF NOT EXISTS ops_alerted_at timestamptz;

COMMENT ON COLUMN pipeline.schedule_audit_anomalies.ops_alerted_at IS
  'When this anomaly appeared in a successfully sent ops alert email (NULL = still owed; reset on reopen/re-break)';

-- Backfill: every pre-existing row was already covered by an ops email when
-- first seen (or predates the queue entirely) - without this, the first run
-- after deploy would re-announce the whole open backlog.
UPDATE pipeline.schedule_audit_anomalies
   SET ops_alerted_at = first_seen_at
 WHERE ops_alerted_at IS NULL;
