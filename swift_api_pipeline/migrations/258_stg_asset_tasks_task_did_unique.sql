-- =============================================================================
-- 258_stg_asset_tasks_task_did_unique.sql
-- WAL diet item 4 (2026-09-09): stg_asset_tasks is merged IN PLACE by
-- transform.py transform_asset_tasks (delete gone, write new/changed) instead
-- of `WITH cleared AS (DELETE ..) INSERT ..` of all ~2.79M rows per run
-- (4.3 GB of WAL per run, two runs a day since the 6 PM PHT chain).
-- The merge's ON CONFLICT (task_did) needs a UNIQUE index. Byte-identical
-- duplicate task_did rows have landed before (25 on 2026-05-15), so the guard
-- refuses to apply while any exist; the transform de-duplicates with
-- DISTINCT ON (task_did) from now on. The non-unique idx_stg_asset_tasks_task_did
-- becomes redundant and is dropped.
--
-- Apply OUTSIDE the two reload windows (12:01-1:00 AM ET, 6:05-6:45 AM ET):
-- CREATE INDEX takes a SHARE lock (~1-2 min on 2.79M rows) that would block
-- the transform's writes. Safe with the OLD transform (a full reload never
-- produces duplicates after DISTINCT ON) so it can precede the code deploy.
-- =============================================================================

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM data_staging.stg_asset_tasks
    WHERE task_did IS NOT NULL
    GROUP BY task_did HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'stg_asset_tasks has duplicate task_did rows; de-duplicate before applying 258';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_stg_asset_tasks_task_did
  ON data_staging.stg_asset_tasks (task_did);

DROP INDEX IF EXISTS data_staging.idx_stg_asset_tasks_task_did;
