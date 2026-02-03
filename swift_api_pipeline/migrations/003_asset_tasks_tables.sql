-- migrations/003_asset_tasks_tables.sql
-- Raw and staging tables for asset-tasks

-- Raw table for asset-tasks (individual JSONB rows)
CREATE TABLE IF NOT EXISTS raw_asset_tasks (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    project_did TEXT NOT NULL,
    data JSONB NOT NULL
);

-- Indexes for raw_asset_tasks
CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_loaded_at ON raw_asset_tasks(loaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_run_id ON raw_asset_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_project_did ON raw_asset_tasks(project_did);
CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_data ON raw_asset_tasks USING GIN(data);

-- Staging table for asset-tasks (flattened columns matching API response)
CREATE TABLE IF NOT EXISTS stg_asset_tasks (
    id BIGSERIAL PRIMARY KEY,
    -- DID fields
    project_did TEXT,  -- FK to stg_projects.project_did
    project_status TEXT,
    asset_did TEXT,
    task_did TEXT,
    -- Asset info
    asset_id TEXT,
    asset_name TEXT,
    asset_requirement_count INTEGER,
    -- Task info
    task_name TEXT,
    task_status TEXT,
    task_scheduled DATE,
    -- Assignment
    task_assigned_to_did TEXT,
    task_assigned_to_collection TEXT,
    task_assigned_to_name TEXT,
    task_assigned_to_email TEXT,
    -- Submitted
    task_submitted_on DATE,
    task_submitted_by_did TEXT,
    task_submitted_by_name TEXT,
    task_submitted_by_email TEXT,
    -- Approved
    task_approved_on DATE,
    task_approved_by_did TEXT,
    task_approved_by_name TEXT,
    task_approved_by_email TEXT,
    -- Cancelled
    task_cancelled_on DATE,
    task_cancelled_by_did TEXT,
    task_cancelled_by_name TEXT,
    task_cancelled_by_email TEXT,
    -- Metadata
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for stg_asset_tasks
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_project_did ON stg_asset_tasks(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_task_did ON stg_asset_tasks(task_did);
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_asset_did ON stg_asset_tasks(asset_did);
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_task_status ON stg_asset_tasks(task_status);
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_run_id ON stg_asset_tasks(run_id);

-- FK constraint to connect to stg_projects
ALTER TABLE stg_asset_tasks
ADD CONSTRAINT fk_stg_asset_tasks_project
FOREIGN KEY (project_did) REFERENCES stg_projects(project_did);
