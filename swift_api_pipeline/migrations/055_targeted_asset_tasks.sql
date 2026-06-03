-- migrations/055_targeted_asset_tasks.sql
-- Targeted asset-task extraction for report-driven data needs.
--
-- Background: data_raw.raw_asset_tasks_gc uses the heavy Swift
-- /assets/_export endpoint (10-200 KB JSONB per asset row) and is sized
-- for full-GC sweeps (~11M rows). For specific report use cases that
-- only need flat task-level data scoped to a known list of projects,
-- that endpoint is overkill and slow.
--
-- This migration adds a lighter pattern mirroring the Asset Export tool:
--   /api/projects/{p}/assets             → asset metadata (~500 bytes/row)
--   /api/asset-projects/{a}/asset-tasks  → flat task-level fields
--
-- Layout:
--   reference.report_targets             → config: which (report, org, project) tuples to extract
--   data_staging.stg_targeted_asset_tasks → output: one row per task with denormalized
--                                            org + project + asset context
--
-- Workflow:
--   1. INSERT rows into reference.report_targets for a new report scope
--   2. Run `python main.py --pipeline targeted_asset_tasks`
--   3. Per-report rows in stg_targeted_asset_tasks are TRUNCATEd+reloaded each run
--   4. Reports query WHERE report_name = '<scope>' for their data

BEGIN;

-- 1. Config table: which (report, org, project) tuples to extract
CREATE TABLE IF NOT EXISTS reference.report_targets (
    id          BIGSERIAL PRIMARY KEY,
    report_name TEXT NOT NULL,
    org_did     TEXT NOT NULL,
    org_name    TEXT,
    project_did TEXT NOT NULL,
    project_name TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes       TEXT,
    UNIQUE (report_name, project_did)
);

CREATE INDEX IF NOT EXISTS idx_report_targets_active
    ON reference.report_targets (report_name)
    WHERE enabled;

COMMENT ON TABLE reference.report_targets IS
    'Configurable (report_name, org, project) tuples consumed by the targeted_asset_tasks pipeline. Add rows via SQL INSERT to expand a report''s scope; set enabled=false to retire without deleting.';

-- 2. Output table: flat task-level rows with denormalized org/project/asset context
CREATE TABLE IF NOT EXISTS data_staging.stg_targeted_asset_tasks (
    id                 BIGSERIAL PRIMARY KEY,
    report_name        TEXT NOT NULL,
    run_id             UUID NOT NULL,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Org + project context
    org_did            TEXT NOT NULL,
    org_name           TEXT,
    project_did        TEXT NOT NULL,
    project_name       TEXT,
    project_status     TEXT,

    -- Asset context
    asset_project_did  TEXT,                 -- corresponds to /api/asset-projects/{X} (per-project asset DID)
    asset_id           TEXT,                 -- canonical asset DID (cross-project)
    asset_identifier   TEXT,
    asset_name         TEXT,
    asset_status       TEXT,

    -- Task fields
    task_did           TEXT NOT NULL,
    task_name          TEXT,
    task_status        TEXT,
    assigned_to        TEXT,
    task_description   TEXT,
    task_url           TEXT
);

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_report
    ON data_staging.stg_targeted_asset_tasks (report_name);

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_run
    ON data_staging.stg_targeted_asset_tasks (run_id);

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_project
    ON data_staging.stg_targeted_asset_tasks (project_did);

CREATE INDEX IF NOT EXISTS idx_targeted_asset_tasks_task
    ON data_staging.stg_targeted_asset_tasks (task_did);

COMMENT ON TABLE data_staging.stg_targeted_asset_tasks IS
    'Flat task-level snapshot rows produced by the targeted_asset_tasks pipeline. One row per task; org/project/asset context denormalized for direct report queries. Per-report rows are TRUNCATE-and-reload (delete WHERE report_name=X, then insert) each pipeline run.';

COMMIT;
