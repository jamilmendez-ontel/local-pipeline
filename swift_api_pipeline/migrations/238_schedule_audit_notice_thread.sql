-- 238: schedule audit notice threading (Jamil, 2026-08-20)
--
-- The member-facing "Schedule needs a quick re-do" notice now gets an
-- all-clear follow-up on the SAME Gmail thread when the anomaly resolves,
-- so members know the fix landed. These columns remember the thread.
-- No RLS changes: the table already has RLS enabled with anon/authenticated
-- revoked (migration 236); columns inherit that.

ALTER TABLE pipeline.schedule_audit_anomalies
  ADD COLUMN IF NOT EXISTS notice_thread_id text,
  ADD COLUMN IF NOT EXISTS notice_message_id text,
  ADD COLUMN IF NOT EXISTS notice_subject text,
  ADD COLUMN IF NOT EXISTS notice_recipients text[],
  ADD COLUMN IF NOT EXISTS notice_sent_at timestamptz,
  ADD COLUMN IF NOT EXISTS resolved_notice_sent_at timestamptz;

COMMENT ON COLUMN pipeline.schedule_audit_anomalies.notice_thread_id IS
  'Gmail threadId of the member-facing notice; the all-clear follow-up replies on this thread';
COMMENT ON COLUMN pipeline.schedule_audit_anomalies.notice_message_id IS
  'RFC 822 Message-ID of the notice (In-Reply-To/References header for the follow-up)';
COMMENT ON COLUMN pipeline.schedule_audit_anomalies.notice_subject IS
  'Subject of the original notice; the follow-up sends "Re: <subject>"';
COMMENT ON COLUMN pipeline.schedule_audit_anomalies.notice_recipients IS
  'Recipients of the original notice; the follow-up goes to the same set';
COMMENT ON COLUMN pipeline.schedule_audit_anomalies.notice_sent_at IS
  'When the member-facing notice went out (NULL = never sent, e.g. pre-2026-08-20 or ops-only)';
COMMENT ON COLUMN pipeline.schedule_audit_anomalies.resolved_notice_sent_at IS
  'When the all-clear follow-up went out (NULL = not yet / no original notice)';
