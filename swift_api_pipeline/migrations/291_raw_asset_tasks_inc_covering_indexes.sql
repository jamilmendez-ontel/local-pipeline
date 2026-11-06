-- =============================================================================
-- 291_raw_asset_tasks_inc_covering_indexes.sql
-- OntelDB disk IO: let the GCP walker's per-asset lookup and the project-wide attr
-- heal read the index only, never the 6 GB raw heap.
--
-- STATUS: NOT APPLIED. Apply outside the reload windows (12:01-1:15 AM, 5:05-6:00 AM ET)
--   and outside the 04:30 / 18:30 ET full walks. CREATE/DROP INDEX CONCURRENTLY and VACUUM
--   cannot run in a transaction block: run each statement on its own through MCP
--   execute_sql (not apply_migration), in order, checking indisvalid after each CREATE.
--
-- WHY (pg_stat_statements 2026-11-04 10:02 UTC -> 11-06 ~12:00 UTC)
--   * #1 IO statement: platform_core.asset_walk select_stored_tasks
--       SELECT task_did, last_updated FROM data_raw.raw_asset_tasks_inc WHERE asset_did = $1
--     279,525 calls, 16.4M blocks read (~125 GB) in ~2 days. raw_asset_tasks_inc is 6.2 GB
--     of heap for 2.9M rows (~2 KB/row, jsonb data inline), so the 78 rows of one asset sit
--     on ~50 different pages: EXPLAIN (ANALYZE, BUFFERS) 11-06 = 50 buffers for 78 rows.
--     The asset index only gives the row locations; every row is a heap page read.
--   * #3: the project-wide attr heal (sync_task_attrs, full/baseline walks) joins raw by
--     project_did and needs r.task_did, r.asset_did: 37 calls, 9.45M blocks (~2 GB a call).
--   * OntelDB: 50 GB on 512 MB shared_buffers, heap hit ratio 76%: most of these are disk.
--
-- WHAT
--   1. (asset_did) INCLUDE (task_did, last_updated, project_did) replaces the plain asset
--      index: select_stored_tasks, delete_*_for_assets and the scoped heal
--      (r.asset_did = ANY, r.project_did, r.task_did) become index-only.
--   2. (project_did) INCLUDE (task_did, asset_did) replaces the plain project index: the
--      project-wide heal's raw side becomes index-only.
--   3. Autovacuum on raw_asset_tasks_inc at 1% (default 20% = ~585k rows, it ran once in
--      2 days): index-only scans skip the heap only for all-visible pages, so the
--      visibility map has to stay fresh. stg_assets_inc (11 MB, never vacuumed, 20k heap
--      fetches on an index-only scan) gets the same.
--   4. One VACUUM (ANALYZE) of each to set the visibility map now.
--   Same predicates, same plans otherwise; no view/function/code change.
--   Cost: the indexes are ~2x the plain ones (~+250 MB total); each CREATE reads the heap
--   once (~6 GB), the first VACUUM up to once more.
--
-- ROLLBACK
--   CREATE INDEX CONCURRENTLY idx_raw_asset_tasks_inc_asset   ON data_raw.raw_asset_tasks_inc (asset_did);
--   CREATE INDEX CONCURRENTLY idx_raw_asset_tasks_inc_project ON data_raw.raw_asset_tasks_inc (project_did);
--   DROP INDEX CONCURRENTLY data_raw.idx_raw_asset_tasks_inc_asset_cov;
--   DROP INDEX CONCURRENTLY data_raw.idx_raw_asset_tasks_inc_project_cov;
--   ALTER TABLE data_raw.raw_asset_tasks_inc RESET (autovacuum_vacuum_scale_factor,
--     autovacuum_vacuum_insert_scale_factor, autovacuum_analyze_scale_factor);
--   ALTER TABLE data_staging.stg_assets_inc RESET (autovacuum_vacuum_scale_factor,
--     autovacuum_vacuum_insert_scale_factor);
--
-- VERIFY AFTER
--   select indexrelid::regclass, indisvalid from pg_index
--    where indrelid = 'data_raw.raw_asset_tasks_inc'::regclass;          -- both _cov valid, old two gone
--   explain (analyze, buffers) select task_did, last_updated from data_raw.raw_asset_tasks_inc
--    where asset_did = '<an asset>';   -- Index Only Scan, Heap Fetches ~0, < 10 buffers
-- =============================================================================

-- 1. asset lookup
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_raw_asset_tasks_inc_asset_cov
  ON data_raw.raw_asset_tasks_inc (asset_did) INCLUDE (task_did, last_updated, project_did);
DROP INDEX CONCURRENTLY IF EXISTS data_raw.idx_raw_asset_tasks_inc_asset;

-- 2. project heal
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_raw_asset_tasks_inc_project_cov
  ON data_raw.raw_asset_tasks_inc (project_did) INCLUDE (task_did, asset_did);
DROP INDEX CONCURRENTLY IF EXISTS data_raw.idx_raw_asset_tasks_inc_project;

-- 3. keep the visibility map fresh
ALTER TABLE data_raw.raw_asset_tasks_inc SET (
  autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_insert_scale_factor = 0.01,
  autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE data_staging.stg_assets_inc SET (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_vacuum_insert_scale_factor = 0.02);

-- 4. set it now
VACUUM (ANALYZE) data_raw.raw_asset_tasks_inc;
VACUUM (ANALYZE) data_staging.stg_assets_inc;
