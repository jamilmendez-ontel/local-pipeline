-- migrations/002_staging_tables.sql
-- Staging tables with flattened JSONB columns and DID relationships

-- Staging table for organizations
CREATE TABLE IF NOT EXISTS stg_organizations (
    id BIGSERIAL PRIMARY KEY,
    org_did TEXT UNIQUE NOT NULL,
    org_name TEXT,
    avc INTEGER,
    poc_id TEXT,
    poc_name TEXT,
    poc_email TEXT,
    created_by_id TEXT,
    date_created TIMESTAMPTZ,
    last_updated TIMESTAMPTZ,
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Staging table for projects
CREATE TABLE IF NOT EXISTS stg_projects (
    id BIGSERIAL PRIMARY KEY,
    project_did TEXT UNIQUE NOT NULL,
    project_name TEXT,
    org_did TEXT,  -- JOIN to stg_organizations.org_did
    org_name TEXT,
    status TEXT,
    is_private BOOLEAN,
    location_orientation TEXT,
    -- Asset metrics
    asset_task_count INTEGER,
    asset_task_pending INTEGER,
    asset_task_approved INTEGER,
    asset_task_rejected INTEGER,
    asset_task_cancelled INTEGER,
    asset_task_submitted INTEGER,
    asset_task_in_progress INTEGER,
    asset_project_count INTEGER,
    asset_milestone_count INTEGER,
    -- Project metrics
    project_task_count INTEGER,
    project_task_pending INTEGER,
    project_task_approved INTEGER,
    -- Metadata
    created_by_id TEXT,
    date_created TIMESTAMPTZ,
    last_updated TIMESTAMPTZ,
    metrics_last_updated TIMESTAMPTZ,
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Staging table for user priorities
CREATE TABLE IF NOT EXISTS stg_user_priorities (
    id BIGSERIAL PRIMARY KEY,
    task_did TEXT,
    asset_did TEXT,
    org_did TEXT,  -- JOIN to stg_organizations.org_did
    project_did TEXT,  -- JOIN to stg_projects.project_did
    -- Task info
    task_name TEXT,
    milestone TEXT,
    status TEXT,
    calendar_status TEXT,
    -- Assignment
    assigned_to TEXT,
    scheduled TIMESTAMPTZ,
    scheduled_by TEXT,
    display_date TIMESTAMPTZ,
    duration TEXT,
    pin_type TEXT,
    -- Approval workflow
    submitted_by TEXT,
    submitted_on TIMESTAMPTZ,
    approved_by TEXT,
    approved_on TIMESTAMPTZ,
    rejected_by TEXT,
    rejected_on TIMESTAMPTZ,
    cancelled_by TEXT,
    cancelled_on TIMESTAMPTZ,
    -- Context
    organization TEXT,
    project TEXT,
    asset_id TEXT,
    asset_name TEXT,
    -- Metadata
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for JOINs and common queries
CREATE INDEX IF NOT EXISTS idx_stg_organizations_org_did ON stg_organizations(org_did);
CREATE INDEX IF NOT EXISTS idx_stg_projects_org_did ON stg_projects(org_did);
CREATE INDEX IF NOT EXISTS idx_stg_projects_project_did ON stg_projects(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_projects_status ON stg_projects(status);
CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_org_did ON stg_user_priorities(org_did);
CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_project_did ON stg_user_priorities(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_status ON stg_user_priorities(status);
CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_calendar_status ON stg_user_priorities(calendar_status);
CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_task_did ON stg_user_priorities(task_did);
