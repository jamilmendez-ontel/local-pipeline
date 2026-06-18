-- 108_quote_swift_filings.sql
-- Quote-app Swift write-back: current filing state (1 row per task) + append-only audit log.
CREATE TABLE IF NOT EXISTS data_staging.stg_quote_swift_filings (
  task_did        text PRIMARY KEY,
  requirement_did text,
  swift_file_id   text,
  status          text NOT NULL,            -- uploaded | approved | failed
  last_error      text,
  attempt_count   int  NOT NULL DEFAULT 0,
  filed_by        text,
  filed_at        timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS data_staging.stg_quote_swift_filing_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_did    text NOT NULL,
  actor       text,
  step        text NOT NULL,                -- preflight | upload | approve | login
  outcome     text NOT NULL,                -- success | failure
  http_status int,
  message     text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_qsf_log_task ON data_staging.stg_quote_swift_filing_log (task_did, created_at DESC);
