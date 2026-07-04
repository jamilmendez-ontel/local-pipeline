-- 147: HR Report Review dashboard RPCs. Aggregate analytics.v_hr_report_review
-- server-side so the /hr Report Review page doesn't pull the full row set
-- client-side just to compute KPIs, a trend sparkline, and per-carrier-group
-- late rates (same pattern as 144's approval_queue_summary).
--
-- Hardening block copied from migration 144 (language sql stable, security
-- invoker, explicit search_path, revoke-then-grant-service_role). 144's
-- search_path was `analytics, public`; these functions don't touch public,
-- so per task instructions the search_path is 144's shape extended with the
-- schemas actually read: analytics, data_staging, reference.
--
-- Two corrections applied vs the task brief's Step 1 sketch (found in review):
--   1. hr_review_offenders: the per-employee `matured` denominator
--      (`count(*) FILTER (WHERE is_matured)`) would count cancelled tasks
--      that still carry stale work evidence (clock-in/timer history) as
--      matured, inflating the denominator with rows that were never really
--      "due" a report. Added `AND task_status <> 'cancelled'`.
--   2. hr_review_summary trend CTE: `bool_and(is_matured) FILTER (WHERE
--      has_time_in)` returns SQL NULL for a day with zero has_time_in rows,
--      which would surface as JSON null instead of a boolean in the trend
--      array. Wrapped in COALESCE(..., false).
create or replace function analytics.hr_review_summary(p_from date, p_to date)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  with rows_in as (
    select * from analytics.v_hr_report_review
    where work_date between p_from and p_to
  ), matured as (
    select * from rows_in where is_matured and has_time_in and submitted_on_et is not null
  ), kpis as (
    select
      (select count(*) from matured)                                        as matured,
      (select count(*) from matured where is_late_filing)                   as late,
      (select round(100.0 * count(*) filter (where not is_late_filing) / nullif(count(*),0), 1) from matured) as on_time_pct,
      (select count(*) from rows_in where is_missing_report)                as missing,
      (select round(percentile_cont(0.5) within group (order by filing_lag_hours)::numeric, 1) from matured) as median_lag_hours,
      (select round(percentile_cont(0.9) within group (order by filing_lag_hours)::numeric, 1) from matured) as p90_lag_hours,
      (select count(*) from rows_in
        where coverage_pct is not null and coverage_pct < 75 and variance_hours > 2) as red_variance
  ), trend as (
    select work_date as d,
           count(*) filter (where is_late_filing)    as late,
           count(*) filter (where is_missing_report) as missing,
           coalesce(bool_and(is_matured) filter (where has_time_in), false) as matured
    from rows_in group by work_date order by work_date
  ), grp as (
    select carrier_group, count(*) as n,
           count(*) filter (where is_late_filing) as late,
           round(100.0 * count(*) filter (where is_late_filing) / count(*), 1) as late_pct
    from matured group by carrier_group having count(*) >= 20
    order by late_pct desc
  )
  select jsonb_build_object(
    'kpis',   (select to_jsonb(k) from kpis k),
    'trend',  coalesce((select jsonb_agg(to_jsonb(t)) from trend t), '[]'::jsonb),
    'groups', coalesce((select jsonb_agg(to_jsonb(g)) from grp g), '[]'::jsonb)
  );
$$;

create or replace function analytics.hr_review_offenders(p_from date, p_to date)
returns jsonb
language sql
stable
security invoker
set search_path = analytics, data_staging, reference
as $$
  with per_emp as (
    select emp_id, max(employee_name) as employee_name, max(carrier_group) as carrier_group,
           count(*) filter (where is_matured and task_status <> 'cancelled') as matured,
           count(*) filter (where is_missing_report)                        as missing,
           count(*) filter (where is_late_filing and is_matured)            as late
    from analytics.v_hr_report_review
    where work_date between p_from and p_to
    group by emp_id
  ), scored as (
    select *, round(100.0 * (missing + late) / nullif(matured, 0), 0) as rate_pct
    from per_emp where missing + late > 0
  )
  select jsonb_build_object(
    'offenders', coalesce((select jsonb_agg(to_jsonb(s) order by s.rate_pct desc, s.missing desc)
                           from scored s where s.matured >= 10), '[]'::jsonb),
    'low_volume', (select count(*) from scored where matured < 10)
  );
$$;

revoke all on function analytics.hr_review_summary(date, date) from public, anon, authenticated;
grant execute on function analytics.hr_review_summary(date, date) to service_role;

revoke all on function analytics.hr_review_offenders(date, date) from public, anon, authenticated;
grant execute on function analytics.hr_review_offenders(date, date) to service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','hr_review_summary',
   'Function(p_from date, p_to date): KPIs (matured, late, on_time_pct, missing, median_lag_hours, p90_lag_hours, red_variance) + daily trend (late, missing, matured bool) + per-carrier-group late rate (groups with >=20 matured reports) over analytics.v_hr_report_review for the given date range.',
   'Powers the HR Report Review dashboard KPI strip, trend sparkline, and carrier-group breakdown without the app fetching the full row set client-side.',
   ARRAY['analytics.v_hr_report_review']),
  ('analytics','hr_review_offenders',
   'Function(p_from date, p_to date): per-employee offenders list (emp_id, employee_name, carrier_group, missing, late, matured, rate_pct) for employees with matured>=10 and (missing+late)>0 over analytics.v_hr_report_review, plus low_volume count of employees below the 10-matured threshold. matured excludes cancelled tasks (stale work evidence on a cancelled report should not count toward the denominator).',
   'Powers the HR Report Review dashboard offenders table.',
   ARRAY['analytics.v_hr_report_review'])
ON CONFLICT DO NOTHING;
