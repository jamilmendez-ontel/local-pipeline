-- 177: index raw_asset_tasks_inc.asset_did.
--
-- Migration 176 indexed only project_did on the raw shadow table (the stg
-- table got the asset_did index). When the 2026-07-10 export-semantics fix
-- moved the walker's per-asset stored-task lookups from stg to raw
-- (stg.asset_did now mirrors the current pipeline's underlying asset.id,
-- so raw is the only table keyed by the asset-project id), every lookup
-- became a sequential scan over ~1.2M JSONB rows: 10s+ of DataFileRead per
-- asset, observed live starving concurrent pipelines of disk IO.

create index idx_raw_asset_tasks_inc_asset
  on data_raw.raw_asset_tasks_inc (asset_did);
