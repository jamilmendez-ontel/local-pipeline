-- migrations/009_requirements_tables.sql
-- Raw and staging tables for Asset Task Requirements data
-- Requirements are fetched per-task from /api/asset-tasks/{task_DID}/requirements

-- ============================================================
-- RAW TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS data_raw.raw_asset_task_requirements (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    project_did TEXT NOT NULL,
    task_did TEXT NOT NULL,
    data JSONB NOT NULL
);

-- Indexes for raw table
CREATE INDEX IF NOT EXISTS idx_raw_asset_task_requirements_run_id
    ON data_raw.raw_asset_task_requirements(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_asset_task_requirements_project_did
    ON data_raw.raw_asset_task_requirements(project_did);
CREATE INDEX IF NOT EXISTS idx_raw_asset_task_requirements_task_did
    ON data_raw.raw_asset_task_requirements(task_did);
CREATE INDEX IF NOT EXISTS idx_raw_asset_task_requirements_data
    ON data_raw.raw_asset_task_requirements USING GIN(data);

-- ============================================================
-- STAGING TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS data_staging.stg_asset_task_requirements (
    id BIGSERIAL PRIMARY KEY,
    -- Hierarchy identifiers
    project_did TEXT NOT NULL,
    asset_did TEXT,
    task_did TEXT NOT NULL,
    requirement_did TEXT,
    -- Requirement info
    requirement_name TEXT,
    requirement_type TEXT,
    requirement_status TEXT,
    requirement_description TEXT,
    -- Media/attachments
    has_photo BOOLEAN,
    has_document BOOLEAN,
    photo_count INTEGER,
    document_count INTEGER,
    -- Assignment & workflow
    assigned_to_did TEXT,
    assigned_to_name TEXT,
    assigned_to_email TEXT,
    completed_by_did TEXT,
    completed_by_name TEXT,
    completed_on TIMESTAMPTZ,
    -- Approval workflow
    submitted_on TIMESTAMPTZ,
    submitted_by_did TEXT,
    submitted_by_name TEXT,
    approved_on TIMESTAMPTZ,
    approved_by_did TEXT,
    approved_by_name TEXT,
    rejected_on TIMESTAMPTZ,
    rejected_by_did TEXT,
    rejected_by_name TEXT,
    -- Form data (if requirement is a form response)
    form_id TEXT,
    form_response_id TEXT,
    -- Metadata
    date_created TIMESTAMPTZ,
    last_updated TIMESTAMPTZ,
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for staging table
CREATE INDEX IF NOT EXISTS idx_stg_requirements_project_did
    ON data_staging.stg_asset_task_requirements(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_requirements_asset_did
    ON data_staging.stg_asset_task_requirements(asset_did);
CREATE INDEX IF NOT EXISTS idx_stg_requirements_task_did
    ON data_staging.stg_asset_task_requirements(task_did);
CREATE INDEX IF NOT EXISTS idx_stg_requirements_requirement_did
    ON data_staging.stg_asset_task_requirements(requirement_did);
CREATE INDEX IF NOT EXISTS idx_stg_requirements_status
    ON data_staging.stg_asset_task_requirements(requirement_status);
CREATE INDEX IF NOT EXISTS idx_stg_requirements_run_id
    ON data_staging.stg_asset_task_requirements(run_id);

-- ============================================================
-- TRACKING TABLE FOR INCREMENTAL PROCESSING
-- ============================================================

-- Track which tasks have been processed for requirements
-- Allows incremental extraction rather than re-fetching all tasks
CREATE TABLE IF NOT EXISTS pipeline.requirements_extraction_progress (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    project_did TEXT NOT NULL,
    task_did TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, success, failed, skipped
    requirements_count INTEGER,
    error_message TEXT,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_req_progress_run_id
    ON pipeline.requirements_extraction_progress(run_id);
CREATE INDEX IF NOT EXISTS idx_req_progress_project_did
    ON pipeline.requirements_extraction_progress(project_did);
CREATE INDEX IF NOT EXISTS idx_req_progress_task_did
    ON pipeline.requirements_extraction_progress(task_did);
CREATE INDEX IF NOT EXISTS idx_req_progress_status
    ON pipeline.requirements_extraction_progress(status);

-- Unique constraint to prevent duplicate processing
CREATE UNIQUE INDEX IF NOT EXISTS idx_req_progress_run_task
    ON pipeline.requirements_extraction_progress(run_id, task_did);
