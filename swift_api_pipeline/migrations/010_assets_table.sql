-- migrations/010_assets_table.sql
-- Create separate assets table and update data model
-- Assets are extracted from the bulk export endpoint, split from task data during transform

-- ============================================================
-- ASSETS STAGING TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS data_staging.stg_assets (
    id BIGSERIAL PRIMARY KEY,
    -- Identifiers
    project_did TEXT NOT NULL,
    asset_did TEXT NOT NULL,
    -- Asset info
    asset_id TEXT,              -- Human-readable ID (e.g., "ATL001")
    asset_name TEXT,            -- Site name/location
    -- Counts (denormalized for convenience)
    task_count INTEGER,
    requirement_count INTEGER,
    -- Status (derived from tasks)
    tasks_pending INTEGER,
    tasks_in_progress INTEGER,
    tasks_submitted INTEGER,
    tasks_approved INTEGER,
    tasks_rejected INTEGER,
    tasks_cancelled INTEGER,
    -- Metadata
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Unique constraint
    CONSTRAINT uq_stg_assets_project_asset UNIQUE (project_did, asset_did)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_stg_assets_project_did
    ON data_staging.stg_assets(project_did);
CREATE INDEX IF NOT EXISTS idx_stg_assets_asset_did
    ON data_staging.stg_assets(asset_did);
CREATE INDEX IF NOT EXISTS idx_stg_assets_asset_id
    ON data_staging.stg_assets(asset_id);
CREATE INDEX IF NOT EXISTS idx_stg_assets_run_id
    ON data_staging.stg_assets(run_id);

-- Grant permissions
GRANT ALL ON data_staging.stg_assets TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE data_staging.stg_assets_id_seq TO anon, authenticated, service_role;

-- ============================================================
-- ADD FOREIGN KEY FROM ASSET_TASKS TO ASSETS
-- ============================================================

-- Note: The existing stg_asset_tasks table already has asset_did
-- We can add a FK constraint but it may fail if data doesn't match
-- So we'll just add an index for now and document the relationship

-- Verify the relationship exists conceptually:
-- stg_projects (project_did)
--   └── stg_assets (project_did, asset_did)
--         └── stg_asset_tasks (asset_did, task_did)
--               └── stg_asset_task_requirements (task_did, requirement_did)
