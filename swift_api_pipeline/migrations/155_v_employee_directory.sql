-- 155_v_employee_directory.sql
-- Serving view that backs the Ontel People "Directory" page.
--
-- The Directory is now READ-ONLY and sources the authoritative, auto-synced roster
-- (reference.ref_employees, maintained daily by the roster gap watcher from Ms. Orv's
-- HR sheet). The app reads its serving data from analytics.v_* (same pattern as
-- v_daily_report_approvals), NOT from reference lookups directly, so this view gives the
-- app a stable, app-shaped contract and keeps it decoupled from the raw lookup table.
--
-- One row per employee; ref_employees carries no version history, so neither does this.
-- Applied to prod 2026-07-06 via Supabase MCP.

CREATE OR REPLACE VIEW analytics.v_employee_directory AS
SELECT
  emp_id,
  full_name,
  first_name,
  last_name,
  middle_name,
  nickname,
  email,
  position,
  carrier_group,
  carrier,
  cluster,
  division,
  sub_division,
  work_schedule,
  shift_schedule,
  shift_time_in_pht,
  shift_time_out_pht,
  employment_status,
  immediate_supervisor,
  hire_date,
  regularization_date,
  resignation_date,
  is_active
FROM reference.ref_employees;

COMMENT ON VIEW analytics.v_employee_directory IS
  'Read-only serving view for the Ontel People Directory. Sources the authoritative, auto-synced roster (reference.ref_employees). One row per employee; no version history. Created 2026-07-06.';

GRANT SELECT ON analytics.v_employee_directory TO service_role;
