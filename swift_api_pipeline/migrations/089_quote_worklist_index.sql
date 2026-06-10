-- 089_quote_worklist_index.sql
--
-- Speeds up analytics.v_quote_worklist (and everything built on it, including
-- analytics.v_quote_source_invoice_lines, which the quote-automation Data Source
-- tab reads). That view filters data_staging.stg_asset_tasks (now ~2.6M rows) by
-- task_assigned_to_name = 'Accounting'. The only matching index was on
-- task_assigned_to_name alone, so the planner index-scanned ~38k Accounting rows
-- and heap-fetched EVERY one to evaluate the task_name / task_status filters,
-- keeping only ~270 worklist rows. That scattered heap scan took 8-16s and
-- exceeded the PostgREST 8s statement_timeout (the authenticator login role sets
-- statement_timeout=8s, which stays in effect after SET ROLE service_role), so
-- the Data Source tab failed with "canceling statement due to statement timeout".
--
-- This partial index lets the planner narrow to the matching task_name/task_status
-- rows INSIDE the index, so it heap-fetches only the ~270 survivors. Measured:
--   v_quote_worklist           16,070 ms -> 5.7 ms
--   v_quote_source_invoice_lines 10,047 ms -> 1,578 ms (now well under 8s)
--
-- The index is partial (predicate = the worklist's hardcoded Accounting filter),
-- so it covers only ~38k rows (~296 kB) and adds negligible overhead to the
-- nightly atomic DELETE+INSERT reload of stg_asset_tasks (transform.py). The
-- reload does not drop/recreate the table, so the index persists across runs.
--
-- NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block. Apply
-- this statement on its own (autocommit), not wrapped in BEGIN/COMMIT. It was
-- applied live on 2026-06-10 via CONCURRENTLY; IF NOT EXISTS makes re-application
-- a no-op. On a fresh/empty rebuild the CONCURRENTLY keyword is harmless.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stg_asset_tasks_quote_worklist
ON data_staging.stg_asset_tasks (task_name, task_status)
WHERE task_assigned_to_name = 'Accounting';
