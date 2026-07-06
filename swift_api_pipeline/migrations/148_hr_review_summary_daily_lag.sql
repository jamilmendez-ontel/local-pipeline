-- 148: add per-day median filing lag to analytics.hr_review_summary's trend.
--
-- The /hr dashboard gets a second chart under the late+missing bars: a daily
-- median filing-lag trend (hours to file, against the 48h deadline). The KPI
-- strip already exposes the range-wide median/p90 lag; this adds the same
-- metric resolved per work_date so the shape over time is visible.
--
-- Only re-creates hr_review_summary (offenders is unchanged). The trend CTE
-- gains `med_lag`: the median filing_lag_hours over the SAME matured+filed
-- population the range-wide median_lag_hours KPI uses (is_matured, has_time_in,
-- submitted_on_et not null), computed per day via a FILTER on the ordered-set
-- aggregate. Days with no matured+filed report yield SQL NULL -> JSON null,
-- which the chart renders as a gap (no bar), matching how the KPI treats an
-- empty population.
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
           coalesce(bool_and(is_matured) filter (where has_time_in), false) as matured,
           round(
             (percentile_cont(0.5) within group (order by filing_lag_hours)
               filter (where is_matured and has_time_in and submitted_on_et is not null))::numeric,
             1
           ) as med_lag
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
   SET description = 'Function(p_from date, p_to date): KPIs (matured, late, on_time_pct, missing, median_lag_hours, p90_lag_hours, red_variance) + daily trend (late, missing, matured bool, med_lag = per-day median filing lag hours over matured+filed reports, null when none) + per-carrier-group late rate (groups with >=20 matured reports) over analytics.v_hr_report_review for the given date range.'
 WHERE schema_name = 'analytics' AND table_name = 'hr_review_summary';
