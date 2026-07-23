-- 188: analytics.v_employee_directory gains report_display_name -- the Swift
-- display name written on the employee's most recent daily report, so the
-- ontel-people Directory can show the same name HR reads on the reports (and
-- that DR Monitoring already shows), not the roster's full legal name.
--
-- Swift daily-report assets are named "FullName_<emp_id>" (e.g. "Kyla Palo_241001").
-- migration 163 made v_daily_report_approvals.employee_name PREFER that parse over
-- the roster full_name; this exposes the SAME parse per employee (their latest
-- report) as an additive directory column. The app displays
-- report_display_name and falls back to full_name when it is NULL (a person with
-- no parseable report: new hires, non-DR staff), so nobody loses a name.
--
-- No circular dependency: v_employee_directory and v_daily_report_approvals both
-- derive from reference.ref_employees; the parse here reads data_staging
-- .stg_daily_reports directly (not the approvals view). emp_id is indexed
-- (idx_stg_dr_tasks_emp), so the LATERAL top-1 is a cheap index lookup per row.
--
-- CREATE OR REPLACE contract: the existing column list/order is preserved
-- byte-for-byte; report_display_name is appended last (additive, non-breaking).
-- Rollback: re-create the view without the trailing LATERAL / column.
CREATE OR REPLACE VIEW analytics.v_employee_directory AS
 SELECT e.emp_id,
    e.full_name,
    e.first_name,
    e.last_name,
    e.middle_name,
    e.nickname,
    e.email,
    e."position",
    e.carrier_group,
    e.carrier,
    e.cluster,
    e.division,
    e.sub_division,
    e.work_schedule,
    e.shift_schedule,
    e.shift_time_in_pht,
    e.shift_time_out_pht,
    e.employment_status,
    e.immediate_supervisor,
    e.hire_date,
    e.regularization_date,
    e.resignation_date,
    e.is_active,
    rn.report_display_name
   FROM reference.ref_employees e
   LEFT JOIN LATERAL (
        SELECT "left"(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1) AS report_display_name
        FROM data_staging.stg_daily_reports t
        WHERE t.emp_id = e.emp_id
          AND t.asset_name IS NOT NULL
          AND "right"(t.asset_name, length(t.emp_id) + 1) = ('_'::text || t.emp_id)
          AND t.work_date IS NOT NULL
        ORDER BY t.work_date DESC
        LIMIT 1
   ) rn ON true;

NOTIFY pgrst, 'reload schema';
