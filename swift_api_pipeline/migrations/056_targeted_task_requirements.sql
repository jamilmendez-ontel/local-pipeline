-- migrations/056_targeted_task_requirements.sql
-- Requirement-level data for targeted reports.
--
-- Companion to 055_targeted_asset_tasks. The Open Items Report (and any
-- future "punch items / drill-down" reports) needs to show requirements
-- nested under tasks, not just task-level info.
--
-- The extractor fetches requirements per task via:
--     GET /api/asset-tasks/{task_did}/requirements
-- using the task_dids returned from stg_user_priorities filtered by the
-- report's scope (`reference.report_targets` + `task_name ILIKE
-- '%punch%'` + open status + assigned).
--
-- Workflow:
--   1. INSERT report scope into reference.report_targets (already exists)
--   2. Run `python main.py --pipeline targeted_task_requirements`
--   3. Per-report rows TRUNCATE-and-reload each pipeline run
--   4. Reports JOIN stg_user_priorities ON task_did to get the parent task

BEGIN;

CREATE TABLE IF NOT EXISTS data_staging.stg_targeted_task_requirements (
    id                      BIGSERIAL PRIMARY KEY,
    report_name             TEXT NOT NULL,
    run_id                  UUID NOT NULL,
    loaded_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    task_did                TEXT NOT NULL,           -- joins to stg_user_priorities.task_did

    requirement_name        TEXT,
    requirement_status      TEXT,
    requirement_description TEXT,
    requirement_assigned_to TEXT                     -- from r.assetTask.assignedTo.name per API
);

CREATE INDEX IF NOT EXISTS idx_targeted_task_requirements_report
    ON data_staging.stg_targeted_task_requirements (report_name);

CREATE INDEX IF NOT EXISTS idx_targeted_task_requirements_task
    ON data_staging.stg_targeted_task_requirements (task_did);

CREATE INDEX IF NOT EXISTS idx_targeted_task_requirements_run
    ON data_staging.stg_targeted_task_requirements (run_id);

COMMENT ON TABLE data_staging.stg_targeted_task_requirements IS
    'Requirement-level snapshot rows produced by the targeted_task_requirements pipeline. One row per (task, requirement). Filtered to requirement_status IN (pending, in_progress) at extract time. Per-report rows are TRUNCATE-and-reload (delete WHERE report_name=X, then insert) each pipeline run. JOIN to data_staging.stg_user_priorities ON task_did to get the parent task context.';

COMMIT;
