-- migrations/007_timer_tables.sql
-- Raw and staging tables for Timer Activities data

-- ============================================================
-- RAW TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS raw_timer_activities (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    run_date DATE NOT NULL,  -- Date range identifier for incremental loads
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    project_did TEXT NOT NULL,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_timer_activities_run_id ON raw_timer_activities(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_timer_activities_run_date ON raw_timer_activities(run_date);
CREATE INDEX IF NOT EXISTS idx_raw_timer_activities_project_did ON raw_timer_activities(project_did);
CREATE INDEX IF NOT EXISTS idx_raw_timer_activities_dates ON raw_timer_activities(start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_raw_timer_activities_data ON raw_timer_activities USING GIN(data);

-- ============================================================
-- STAGING TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS stg_timer_activities (
    id BIGSERIAL PRIMARY KEY,
    -- Project info
    project TEXT,
    project_number INTEGER,
    project_did TEXT,
    -- Site info
    site_name TEXT,
    site_id TEXT,
    task TEXT,
    -- Location data
    site_lat NUMERIC,
    site_long NUMERIC,
    user_lat NUMERIC,
    user_long NUMERIC,
    user_accuracy_m NUMERIC,
    site_vs_user_km NUMERIC,
    -- Time data
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    duration_min NUMERIC,
    -- User info
    user_name TEXT,
    user_email TEXT,
    user_role TEXT,
    -- Metadata
    run_id UUID NOT NULL,
    run_date DATE NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for staging table
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_project ON stg_timer_activities(project);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_project_number ON stg_timer_activities(project_number);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_project_did ON stg_timer_activities(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_site_id ON stg_timer_activities(site_id);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_start_time ON stg_timer_activities(start_time);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_user_email ON stg_timer_activities(user_email);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_run_id ON stg_timer_activities(run_id);
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_run_date ON stg_timer_activities(run_date);
