-- 047_raw_assets_and_status.sql
-- Adds the raw and staging columns needed for the new assets-extract pipeline.
--
-- Background: Swift's /api/asset-tasks/_report endpoint (used by extract_asset_tasks)
-- returns per-task rows but does NOT include asset-level status. To check whether
-- an asset has been cancelled within a project, we hit /api/projects/{id}/assets
-- which returns one row per (project, asset) with `status`.
--
-- data_raw.raw_assets   : one row per (project_did, asset_did) populated from API
-- stg_assets.asset_status: enriched during transform by joining raw_assets

-- ============================================================================
-- 1. data_raw.raw_assets
-- ============================================================================

CREATE TABLE IF NOT EXISTS data_raw.raw_assets (
    project_did       TEXT        NOT NULL,
    asset_did         TEXT        NOT NULL,
    asset_status      TEXT,
    asset_short_name  TEXT,
    asset_identifier  TEXT,
    raw_data          JSONB,
    run_id            UUID,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_did, asset_did)
);

CREATE INDEX IF NOT EXISTS idx_raw_assets_asset_did
    ON data_raw.raw_assets (asset_did);

CREATE INDEX IF NOT EXISTS idx_raw_assets_status
    ON data_raw.raw_assets (asset_status);

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON data_raw.raw_assets TO service_role;

-- ============================================================================
-- 2. stg_assets.asset_status
-- ============================================================================

ALTER TABLE data_staging.stg_assets
    ADD COLUMN IF NOT EXISTS asset_status TEXT;

CREATE INDEX IF NOT EXISTS idx_stg_assets_asset_status
    ON data_staging.stg_assets (asset_status)
    WHERE asset_status IS NOT NULL;
