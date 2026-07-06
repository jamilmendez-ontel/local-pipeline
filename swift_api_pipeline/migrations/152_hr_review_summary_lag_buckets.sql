-- 152: hr_review_summary trend — add per-day filing-SPEED buckets so the lag
-- chart can be a plain traffic-light stack (share of that day's filed reports
-- done within 24h / 24-48h / over 48h) instead of a median+p90 percentile band,
-- which read as too statistical for the HR audience.
--
-- Adds three counts to the trend CTE over the same matured+filed non-cancelled
-- population as filed_n (so lag_le24 + lag_24_48 + lag_over48 = filed_n):
--   lag_le24   filing_lag_hours < 24
--   lag_24_48  24 <= filing_lag_hours < 48
--   lag_over48 filing_lag_hours >= 48
-- med_lag / p90_lag stay (KPI band still uses the range-wide median/p90) but the
-- chart no longer reads them. Everything else is unchanged from 149.
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
    select * from rows_in
    where is_matured and has_time_in and submitted_on_et is not null
      and task_status <> 'cancelled'
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
           count(*) filter (
             where is_late_filing and is_matured and has_time_in
               and submitted_on_et is not null and task_status <> 'cancelled'
           ) as late,
           count(*) filter (where is_missing_report) as missing,
           count(*) filter (
             where is_matured and has_time_in
               and submitted_on_et is not null and task_status <> 'cancelled'
           ) as filed_n,
           count(*) filter (
             where is_matured and has_time_in and submitted_on_et is not null
               and task_status <> 'cancelled' and filing_lag_hours < 24
           ) as lag_le24,
           count(*) filter (
             where is_matured and has_time_in and submitted_on_et is not null
               and task_status <> 'cancelled' and filing_lag_hours >= 24 and filing_lag_hours < 48
           ) as lag_24_48,
           count(*) filter (
             where is_matured and has_time_in and submitted_on_et is not null
               and task_status <> 'cancelled' and filing_lag_hours >= 48
           ) as lag_over48,
           coalesce(bool_and(is_matured) filter (where has_time_in), false) as matured,
           round(
             (percentile_cont(0.5) within group (order by filing_lag_hours)
               filter (where is_matured and has_time_in
                 and submitted_on_et is not null and task_status <> 'cancelled'))::numeric,
             1
           ) as med_lag,
           round(
             (percentile_cont(0.9) within group (order by filing_lag_hours)
               filter (where is_matured and has_time_in
                 and submitted_on_et is not null and task_status <> 'cancelled'))::numeric,
             1
           ) as p90_lag
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
    'trend',  coalesce((select jsonb_agg(to_jsonb(t) order by t.d) from trend t), '[]'::jsonb),
    'groups', coalesce((select jsonb_agg(to_jsonb(g) order by g.late_pct desc) from grp g), '[]'::jsonb)
  );
$$;

revoke all on function analytics.hr_review_summary(date, date) from public, anon, authenticated;
grant execute on function analytics.hr_review_summary(date, date) to service_role;

UPDATE agent.schema_metadata
   SET description = 'Function(p_from date, p_to date): KPIs (matured, late, on_time_pct, missing, median_lag_hours, p90_lag_hours, red_variance; matured excludes cancelled) + daily trend (late, missing, filed_n denominator, filing-speed buckets lag_le24 / lag_24_48 / lag_over48 summing to filed_n, matured bool, med_lag + p90_lag) + per-carrier-group late rate (>=20 matured) over analytics.v_hr_report_review for the range.'
 WHERE schema_name = 'analytics' AND table_name = 'hr_review_summary';
