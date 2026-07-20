-- 185: stg_assets_inc carries the exact values task rows denormalize.
--
-- Why: stg_asset_tasks_inc denormalizes asset_name (= asset shortName),
-- project_status (= the asset-project's status), asset_id and
-- asset_requirement_count onto every task row, but the walk only rewrites
-- rows of CHANGED assets — renames/status flips leave untouched task rows
-- stale forever (doctrine audit 2026-07-20: 9.5k stale project_status rows
-- on TS19 alone). stg_assets_inc is upserted fresh for ALL assets every
-- walk, but stored `name` (project-prefixed) not `shortName`, and no
-- status — so it couldn't source a sync. These two columns close that gap;
-- swift-data-platform's walk populates them and bulk-syncs task rows from
-- them (guarded, per project).
--
-- Applied: 2026-07-20 (additive, safe on live tables; columns NULL until
-- the next v2 walk populates them).

ALTER TABLE data_staging.stg_assets_inc
  ADD COLUMN IF NOT EXISTS asset_short_name text,
  ADD COLUMN IF NOT EXISTS asset_status text;

COMMENT ON COLUMN data_staging.stg_assets_inc.asset_short_name IS
  'Asset shortName — the exact value stg_asset_tasks_inc.asset_name denormalizes; sync source for the walk''s attr refresh';
COMMENT ON COLUMN data_staging.stg_assets_inc.asset_status IS
  'Asset-project status — the exact value stg_asset_tasks_inc.project_status denormalizes; sync source for the walk''s attr refresh';
