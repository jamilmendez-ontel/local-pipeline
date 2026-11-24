-- =============================================================================
-- 295_v_employee_shifts.sql
-- My Timers shift-end reminder claim still times out under batch load (OnPoint INIT-1306):
-- a lean roster view without v_employee_directory's newest-daily-report lookup.
--
-- STATUS: NOT APPLIED. Apply outside the reload windows (12:01-1:20 AM, 5:00-5:35 AM ET)
--   and outside 09:30-10:30 UTC, through MCP apply_migration (plain DDL, one transaction).
--   Apply BEFORE the ontel-my-timers migration that reads it (its migrate-production run
--   fails on a missing view, nothing half-applied).
--
-- WHY
--   * After migration 289 the claim still hit the anon 3 s limit 1-2 times a day:
--     10 x 500 between 2026-11-05 and 11-11; 2026-11-24 05:20 ET "Timeout was reached" and
--     05:25 ET 500, both inside the second asset-tasks extract (05:07-05:23 ET) + AR aging +
--     sales + timer clean. pg_stat_statements since the 11-20 restart: 1,172 claim calls,
--     mean 128 ms, max 2,952 ms.
--   * EXPLAIN ANALYZE of the claim's select (live, 2026-11-24 ~7 AM ET): 81 ms, 162 pages
--     read from disk. 116 of them are the per-person newest stg_daily_reports lookup
--     (76 people x Limit 1, the asset_name filter needs heap pages). Leaving the column out
--     of the query does not help: the planner keeps the lateral join (measured again, still
--     76 loops). With the lookup off: 9.9 ms, 1 page read.
--   * The claim needs the lookup's output (report_display_name) only as a first-name
--     fallback when ref_employees.first_name is blank (0 of 97 active people today), and
--     only for rows it claims (0 on most runs, at most 20). The app reads the fallback from
--     v_employee_directory for those rows only.
--   * A view, not a grant on reference.ref_employees: my_timers_owner reads the warehouse
--     through analytics views and two reference lookups; this keeps that contract and
--     exposes only the roster columns the reminder needs.
--
-- PREFLIGHT
--   * New object, nothing depends on it; no existing view or function changes.
--   * ref_employees.emp_id is unique (ref_employees_emp_id_key); 97 active rows.
--   * analytics default privileges give service_role only; anon/authenticated get nothing.
--
-- ROLLBACK (after the ontel-my-timers function is rolled back to read v_employee_directory)
--   DROP VIEW IF EXISTS analytics.v_employee_shifts;
--
-- VERIFY AFTER
--   select has_table_privilege('my_timers_owner', 'analytics.v_employee_shifts', 'SELECT');  -- true
--   select has_table_privilege('anon', 'analytics.v_employee_shifts', 'SELECT');             -- false
--   select count(*) from analytics.v_employee_shifts where is_active;                         -- = ref_employees active
-- =============================================================================

CREATE VIEW analytics.v_employee_shifts AS
SELECT e.emp_id,
       e.full_name,
       e.first_name,
       e.email,
       e.shift_time_in_pht,
       e.shift_time_out_pht,
       e.is_active
FROM reference.ref_employees e;

COMMENT ON VIEW analytics.v_employee_shifts IS
  'Roster shift times per employee (ref_employees columns only, no daily-report lookup). '
  'Read by My Timers shift_reminders_due every 5 minutes; use v_employee_directory when the '
  'report display name is needed. Migration 295.';

REVOKE ALL ON analytics.v_employee_shifts FROM PUBLIC, anon, authenticated;
GRANT SELECT ON analytics.v_employee_shifts TO my_timers_owner;
