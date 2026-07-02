-- 142: durable bulk-approve job tables + claim/recompute functions (app_hr).
-- A batch is a set of daily-report approvals an approver kicked off. Items carry
-- per-report status so the run is durable and resumable across navigation/reload.

CREATE TABLE app_hr.approval_batch (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  approver_email text NOT NULL,
  status         text NOT NULL DEFAULT 'running' CHECK (status IN ('running','done')),
  total          int  NOT NULL,
  approved_count int  NOT NULL DEFAULT 0,
  failed_count   int  NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- at most one running batch per approver
CREATE UNIQUE INDEX approval_batch_one_active
  ON app_hr.approval_batch (approver_email) WHERE status = 'running';

CREATE TABLE app_hr.approval_batch_item (
  batch_id         uuid NOT NULL REFERENCES app_hr.approval_batch(id) ON DELETE CASCADE,
  task_did         text NOT NULL,
  status           text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','approved','failed')),
  attempts         int  NOT NULL DEFAULT 0,
  retryable        boolean NOT NULL DEFAULT false,
  last_reason      text,
  last_http_status int,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (batch_id, task_did)
);

CREATE INDEX approval_batch_item_by_status ON app_hr.approval_batch_item (batch_id, status);

ALTER TABLE app_hr.approval_batch      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_hr.approval_batch_item ENABLE ROW LEVEL SECURITY;
-- no policies: anon/authenticated are denied; the app uses the service role, which bypasses RLS.

-- Atomically claim the next queued items (queued -> processing), self-healing any
-- rows stuck in processing for over 2 minutes (a crashed driver). SKIP LOCKED so two
-- open tabs never claim the same items. Returns the claimed task_dids.
CREATE FUNCTION app_hr.claim_approval_items(p_batch_id uuid, p_limit int)
RETURNS SETOF text
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE app_hr.approval_batch_item
    SET status = 'queued', updated_at = now()
    WHERE batch_id = p_batch_id AND status = 'processing'
      AND updated_at < now() - interval '2 minutes';

  RETURN QUERY
  UPDATE app_hr.approval_batch_item
    SET status = 'processing', updated_at = now()
    WHERE (batch_id, task_did) IN (
      SELECT batch_id, task_did FROM app_hr.approval_batch_item
      WHERE batch_id = p_batch_id AND status = 'queued'
      ORDER BY task_did
      LIMIT p_limit
      FOR UPDATE SKIP LOCKED
    )
    RETURNING task_did;
END $$;

-- Recompute the batch counts and flip to 'done' when nothing is left to process.
CREATE FUNCTION app_hr.recompute_batch(p_batch_id uuid)
RETURNS TABLE (total int, approved_count int, failed_count int, remaining int, status text)
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE app_hr.approval_batch b SET
    approved_count = (SELECT count(*) FROM app_hr.approval_batch_item i WHERE i.batch_id = b.id AND i.status = 'approved'),
    failed_count   = (SELECT count(*) FROM app_hr.approval_batch_item i WHERE i.batch_id = b.id AND i.status = 'failed'),
    status = CASE WHEN NOT EXISTS (
        SELECT 1 FROM app_hr.approval_batch_item i WHERE i.batch_id = b.id AND i.status IN ('queued','processing')
      ) THEN 'done' ELSE 'running' END,
    updated_at = now()
  WHERE b.id = p_batch_id;

  RETURN QUERY
  SELECT b.total, b.approved_count, b.failed_count,
    (SELECT count(*)::int FROM app_hr.approval_batch_item i WHERE i.batch_id = b.id AND i.status IN ('queued','processing')) AS remaining,
    b.status
  FROM app_hr.approval_batch b WHERE b.id = p_batch_id;
END $$;

REVOKE ALL ON FUNCTION app_hr.claim_approval_items(uuid, int) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_hr.recompute_batch(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_hr.claim_approval_items(uuid, int) TO service_role;
GRANT EXECUTE ON FUNCTION app_hr.recompute_batch(uuid) TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('app_hr','approval_batch',
   'Header for one durable bulk daily-report approval run by an approver. Tracks status (running/done) and approved/failed counts. One running batch per approver (partial unique index).',
   'Backs the resumable bulk-approve in Ontel People: progress survives navigation/reload.',
   ARRAY['app_hr.approval_batch_item']),
  ('app_hr','approval_batch_item',
   'One daily report inside a bulk-approve batch, with per-report status (queued/processing/approved/failed), attempts, retryability and last failure reason.',
   'Lets a bulk approve retry transient failures and list residual failures per report.',
   ARRAY['app_hr.approval_batch'])
ON CONFLICT DO NOTHING;
