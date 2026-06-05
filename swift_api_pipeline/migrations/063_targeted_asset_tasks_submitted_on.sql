-- 063_targeted_asset_tasks_submitted_on.sql
-- Add task_submitted_on to stg_targeted_asset_tasks.
--
-- Background: the Open Items Report's Final COP date (BetaSites
-- lookup) currently uses task_approved_on (when the manager approved
-- the COP). Switching to task_submitted_on so the report shows when
-- the field tech actually FINISHED the work, not when it got
-- rubber-stamped. The filter (task_status = 'approved') stays the
-- same — we just display an earlier date for the same set of sites.
--
-- The extractor captures `submittedOn` (epoch ms) from Swift's
-- /api/asset-projects/{a}/asset-tasks endpoint per task; this column
-- holds the converted DATE so report SQL can JOIN/filter cheaply.
--
-- task_approved_on remains in the table — other reports may want it.

ALTER TABLE data_staging.stg_targeted_asset_tasks
    ADD COLUMN IF NOT EXISTS task_submitted_on DATE;

COMMENT ON COLUMN data_staging.stg_targeted_asset_tasks.task_submitted_on IS
    'Date the task was submitted for review in Swift. NULL for tasks not yet submitted. Sourced from Swift''s `submittedOn` epoch field, converted to America/New_York date.';

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_submitted
    ON data_staging.stg_targeted_asset_tasks (project_did, task_submitted_on)
    WHERE task_submitted_on IS NOT NULL;
