-- 149: hr_review_summary — per-day denominator + per-day p90 lag, and a
-- cancelled-task consistency fix. Powers the /hr dashboard's rate-based
-- late+missing chart and the median+p90 filing-lag band.
--
-- Changes vs 148:
--   1. CANCELLED GUARD (correctness): the `matured` population (which feeds
--      late, on_time_pct, median/p90 lag, and the carrier-group rates) now
--      excludes task_status = 'cancelled', matching hr_review_offenders (147).
--      Before this, a cancelled-but-submitted task with stale clock-in evidence
--      counted toward those metrics, so the KPI band and the offenders table
--      used different denominators.
--   2. trend.filed_n: per-day count of the matured+filed (non-cancelled)
--      population — the denominator the dashboard needs to turn per-day late/
--      missing COUNTS into a RATE that's comparable across days (weekends have
--      far fewer due reports). Expected-to-file that day = filed_n + missing.
--   3. trend.p90_lag: per-day 90th-percentile filing lag, so the lag chart can
--      show the tail (median + p90 band), not just the median. Null when a day
--      has no matured+filed report, same as med_lag.
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
   SET description = 'Function(p_from date, p_to date): KPIs (matured, late, on_time_pct, missing, median_lag_hours, p90_lag_hours, red_variance; matured population excludes cancelled) + daily trend (late, missing, filed_n = per-day matured+filed non-cancelled denominator, matured bool, med_lag + p90_lag = per-day median/p90 filing lag hours, null when none) + per-carrier-group late rate (groups with >=20 matured reports) over analytics.v_hr_report_review for the given date range.'
 WHERE schema_name = 'analytics' AND table_name = 'hr_review_summary';
