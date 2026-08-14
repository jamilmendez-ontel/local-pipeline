-- 234: function-scoped work_mem for the two remaining pipeline temp-spillers (Jamil 2026-08-13).
--
-- Same pattern as 233 (proven: refresh_timer_day_rollup_safe 169 MB/call -> 0).
--
-- 1) data_raw.aggregate_assets_from_raw(p_run_id): nightly GROUP BY over the full
--    raw_asset_tasks payload was spilling ~4.8 GB temp PER CALL at default work_mem
--    (3500kB) - the Partial HashAggregate only holds ~16k asset groups (a few MB),
--    but at 3.5 MB it overflowed and rewrote input tuples to disk in batches.
--    EXPLAIN at 64 MB keeps the same HashAggregate plan; groups fit fully in memory.
--    EXECUTE is postgres-only; runs once nightly, single-flight.
--
-- 2) analytics.refresh_one_mv(p_view_name): CONCURRENTLY refreshes for 11 MVs
--    (largest mv_daily_completion, 233 MB total) spilled ~105 MB temp/call x 370
--    calls. EXECUTE: postgres + service_role (quote-automation); two different MVs
--    can refresh concurrently, so worst case is two backends at elevated work_mem -
--    fine on this tier.
--
-- Existing proconfig (statement_timeout 600s / 300s) is preserved; SET clauses are
-- per-parameter. Function-scoped: no other session or query inherits the setting.
-- NOT touched (checked and deliberately skipped): raw_asset_tasks per-project
-- COUNT/DELETE/extract reads are whole-partition by design (LIST-partitioned,
-- migration 052) - no index would reduce them; the stg_asset_tasks full swap and
-- EXCEPT audit diffs die at the incremental cutover.

ALTER FUNCTION data_raw.aggregate_assets_from_raw(p_run_id text) SET work_mem = '64MB';
ALTER FUNCTION analytics.refresh_one_mv(p_view_name text) SET work_mem = '64MB';

-- Verify:
--   SELECT p.proname, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
--   WHERE (n.nspname='data_raw' AND p.proname='aggregate_assets_from_raw')
--      OR (n.nspname='analytics' AND p.proname='refresh_one_mv');
-- Then after the next nightly: temp_blks_written deltas in pg_stat_statements for
-- queryids 4014190551326455646 (aggregate) and -7877592547735937013 (refresh_one_mv).
--
-- Rollback:
--   ALTER FUNCTION data_raw.aggregate_assets_from_raw(p_run_id text) RESET work_mem;
--   ALTER FUNCTION analytics.refresh_one_mv(p_view_name text) RESET work_mem;
