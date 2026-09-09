-- =============================================================================
-- 256_stg_user_priorities_task_did_unique.sql
-- WAL diet (2026-09-08): stg_user_priorities is now merged IN PLACE by
-- transform.py transform_user_priorities (only new / changed / gone rows are
-- written) instead of DELETE + INSERT of all ~11.9k rows every 5 minutes
-- (measured 2026-09-04..08: 20.8 GB of WAL over 1,220 reloads).
--
-- The merge's ON CONFLICT (task_did) needs a UNIQUE index. task_did was
-- already unique in practice: the transform de-duplicates with DISTINCT ON,
-- and 0 duplicates / 0 NULLs were measured on 2026-09-08. The guard below
-- refuses to apply if that ever stops being true, so a partial reload can
-- never be locked in by this index.
--
-- The existing non-unique idx_stg_user_priorities_task_did becomes redundant
-- and is dropped (one less index to maintain on every write).
--
-- Safe with the OLD transform too (its clear+reload never produces
-- duplicates), so this can be applied before the code deploys.
-- =============================================================================

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM data_staging.stg_user_priorities
    WHERE task_did IS NOT NULL
    GROUP BY task_did HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'stg_user_priorities has duplicate task_did rows; de-duplicate before applying 256';
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_stg_user_priorities_task_did
  ON data_staging.stg_user_priorities (task_did);

DROP INDEX IF EXISTS data_staging.idx_stg_user_priorities_task_did;
