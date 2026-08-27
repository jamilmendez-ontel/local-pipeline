-- 246_holiday_watch_coverage.sql
-- holiday_watch_runs: publish-time coverage instead of a numeric watermark.
--
-- Why (pre-merge review of PR #52, 2026-08-27): the Official Gazette RSS is ordered by
-- publish time, not proclamation number, and numbers are posted out of order and late
-- (1391 posted after 1392; 1404 absent while 1405-1409 were up). A "highest number seen"
-- watermark would skip any late-posted proclamation forever. The watcher now records, per
-- real run, the instant up to which every post has been scanned (coverage_ts) and the
-- proclamation keys it scanned ("year:number"); the next run walks back to coverage_ts
-- minus a slack window and treats a key as new when no earlier run scanned it.
-- og_watermark / og_watermark_year stay as informational "highest key seen this run".
--
-- Rollback:
--   ALTER TABLE pipeline.holiday_watch_runs DROP COLUMN scanned_keys, DROP COLUMN coverage_ts;

BEGIN;

ALTER TABLE pipeline.holiday_watch_runs
    ADD COLUMN IF NOT EXISTS scanned_keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS coverage_ts  timestamptz;

COMMENT ON COLUMN pipeline.holiday_watch_runs.scanned_keys IS
  'Proclamation keys ("year:number") this run scanned; the union over real runs in the memory window is what "already reviewed" means.';
COMMENT ON COLUMN pipeline.holiday_watch_runs.coverage_ts IS
  'Every Official Gazette post published before this instant has been scanned by this run (run start when the walk reached its cutoff; the oldest post seen when it did not).';

-- The deploy-time seed row (2026-08-27) scanned the feed back to 2026-07-29: cover to now.
UPDATE pipeline.holiday_watch_runs
   SET coverage_ts = now()
 WHERE coverage_ts IS NULL AND NOT dry_run AND status <> 'error' AND og_watermark = 1409;

UPDATE agent.schema_metadata
   SET description = 'One row per run of holiday_feed_watcher.py: Official Gazette posts scanned (scanned_keys) and the publish instant covered (coverage_ts), Nager.Date cross-check, findings emailed. The watcher never edits ref_holidays; it emails proposed SQL for confirmation.',
       updated_at = now()
 WHERE schema_name = 'pipeline' AND table_name = 'holiday_watch_runs' AND column_name IS NULL;

COMMIT;
