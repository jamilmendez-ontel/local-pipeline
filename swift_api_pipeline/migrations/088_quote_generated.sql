-- 088_quote_generated.sql
-- Quote Automation: track which quotes have been GENERATED + uploaded to Drive.
-- Written by the bulk PDF route (one row per uploaded asset). The app shows a
-- "generated" badge + Drive link, and a "stale / regenerate" hint when the entry
-- was edited after it was generated (generated_at < overrides.updated_at).
-- "Return to queue" deletes the Drive file AND this row, so the entry is fully
-- back in the to-do list. Separate from stg_quote_overrides (different lifecycle).
CREATE TABLE IF NOT EXISTS data_staging.stg_quote_generated (
  task_did      text PRIMARY KEY,
  drive_file_id text,
  drive_link    text,
  generated_by  text,
  generated_at  timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_generated TO service_role;
GRANT SELECT ON data_staging.stg_quote_generated TO authenticated;
