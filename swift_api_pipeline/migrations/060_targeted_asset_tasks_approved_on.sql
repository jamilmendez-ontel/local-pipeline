-- migrations/060_targeted_asset_tasks_approved_on.sql
-- Add task_approved_on to stg_targeted_asset_tasks.
--
-- Background: the Open Items Report needs to identify sites whose
-- "Final COP" milestone is already approved (the "BetaSites" concept
-- from the reference Power BI workbook) and EXCLUDE them from the
-- weekly punch-item summary. Previously this list was hand-maintained
-- in the reference workbook's BetaSites sheet.
--
-- The extractor captures `approvedOn` (epoch ms) from Swift's
-- /api/asset-projects/{a}/asset-tasks endpoint per task; this column
-- holds the converted DATE so report SQL can JOIN/filter cheaply.

ALTER TABLE data_staging.stg_targeted_asset_tasks
    ADD COLUMN IF NOT EXISTS task_approved_on DATE;

COMMENT ON COLUMN data_staging.stg_targeted_asset_tasks.task_approved_on IS
    'Date the task was approved in Swift. NULL for tasks not yet approved (i.e. status != approved). Sourced from Swift''s `approvedOn` epoch field, converted to America/New_York date.';

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_approved
    ON data_staging.stg_targeted_asset_tasks (project_did, task_approved_on)
    WHERE task_approved_on IS NOT NULL;
