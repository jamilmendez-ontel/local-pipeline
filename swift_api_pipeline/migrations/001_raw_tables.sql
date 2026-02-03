-- migrations/001_raw_tables.sql

-- Raw API responses stored as individual JSONB rows
CREATE TABLE IF NOT EXISTS raw_user_priorities (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_organizations (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_projects (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

-- Pipeline execution metadata
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    records_extracted INTEGER,
    error_message TEXT,
    metadata JSONB
);

-- Indexes for querying latest data
CREATE INDEX idx_raw_user_priorities_loaded_at ON raw_user_priorities(loaded_at DESC);
CREATE INDEX idx_raw_user_priorities_run_id ON raw_user_priorities(run_id);
CREATE INDEX idx_raw_organizations_loaded_at ON raw_organizations(loaded_at DESC);
CREATE INDEX idx_raw_organizations_run_id ON raw_organizations(run_id);
CREATE INDEX idx_raw_projects_loaded_at ON raw_projects(loaded_at DESC);
CREATE INDEX idx_raw_projects_run_id ON raw_projects(run_id);
CREATE INDEX idx_pipeline_runs_started_at ON pipeline_runs(started_at DESC);

-- GIN indexes for JSONB querying
CREATE INDEX idx_raw_user_priorities_data ON raw_user_priorities USING GIN(data);
CREATE INDEX idx_raw_organizations_data ON raw_organizations USING GIN(data);
CREATE INDEX idx_raw_projects_data ON raw_projects USING GIN(data);
