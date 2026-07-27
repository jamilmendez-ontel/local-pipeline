-- 191: single variance alarm at 16%+ untimed for Ontel DRMC (Jamil 2026-07-27).
-- The amber/yellow variance tier is retired. A report is now RED when 16% or
-- more of its (break-deducted) stated hours are not backed by timer entries --
-- i.e. coverage_pct <= 84 (untimed = 100 - coverage >= 16). Purely the
-- percentage: the old floors (variance_hours > 1 for amber, > 2 for red) are
-- dropped. Everything else is "normal" (no colour). Builds on migration 190
-- (coverage_pct is already computed against the net-of-break stated hours).
--
-- Three serving objects reference the old dual thresholds and are updated to
-- the single coverage_pct <= 84 rule so the on-screen colour, the "High
-- variance" KPI tile (red_variance), the variance flag filter, and the chip
-- counts all agree:
--   * hr_review_page   -- 'variance' + 'variance_red' flag predicates.
--   * hr_review_count  -- same flag predicates.
--   * hr_review_summary -- red_variance KPI count.
-- ('variance' and 'variance_red' now target the same set; both kept so the
-- KPI-tile deep link and the FlagChips chip keep working unchanged.)
--
-- Signatures are UNCHANGED, so these are plain CREATE OR REPLACE (body-only).
-- Base definitions: live pg_get_functiondef as of 2026-07-27 (post-190).
-- Rollback: restore the variance_hours>1/coverage<90 and >2/<75 predicates.

CREATE OR REPLACE FUNCTION analytics.hr_review_page(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_sort_key text DEFAULT NULL::text,
  p_sort_dir text DEFAULT 'desc'::text,
  p_offset integer DEFAULT 0,
  p_limit integer DEFAULT 100,
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[]
)
 RETURNS SETOF analytics.v_hr_report_review
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select v.*
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_dir = 'asc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
      end
    end desc nulls last,
    case when p_sort_key = 'work_date' and p_sort_dir = 'asc' then v.work_date end asc,
    case when p_sort_key is distinct from 'work_date' or p_sort_dir <> 'asc' then v.work_date end desc,
    v.task_did asc
  offset greatest(coalesce(p_offset, 0), 0)
  limit  least(greatest(coalesce(p_limit, 100), 0), 1000)
$function$;

CREATE OR REPLACE FUNCTION analytics.hr_review_count(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[]
)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select count(*)
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
    )
$function$;

CREATE OR REPLACE FUNCTION analytics.hr_review_summary(p_from date, p_to date)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
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
        where coverage_pct is not null and coverage_pct <= 84) as red_variance
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
               and task_status <> 'cancelled' and filing_lag_hours >= 24 and filing_lag_hours < 49
           ) as lag_24_48,
           count(*) filter (
             where is_matured and has_time_in and submitted_on_et is not null
               and task_status <> 'cancelled' and filing_lag_hours >= 49
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
$function$;

NOTIFY pgrst, 'reload schema';
