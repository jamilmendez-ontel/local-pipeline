-- 224: make the "Filing speed per day" chart drillable, and settle the 49h
-- boundary on ONE definition.
--
-- Jamil 2026-08-07, reviewing the two-tone filing-speed chart on localhost:
-- "when i click on the box it moves to dr monitoring but no filter, it should
-- show whats inside that box i click. for example 8/1 it have 14 entries it
-- should show those".
--
-- Today the bar links to /hr/reports?dateFrom=D&dateTo=D, which filters on
-- work_date ONLY. The bar counts a much narrower population, so on 2026-08-01
-- the bar reads 14 and the page shows all 122 roster rows for that date (107
-- never filed, 83 cancelled, 105 without a time-in). The filter runs; it is
-- just far wider than the bar, which reads as "no filter".
--
-- The bar's population is:
--     is_matured AND has_time_in AND submitted_on_et IS NOT NULL
--     AND task_status <> 'cancelled'                                  ("filed")
-- split into late / not-late. `late` is already expressible as a flag; the
-- on-time half is not. This adds it.
--
-- ---------------------------------------------------------------------------
-- The boundary bug this also fixes
-- ---------------------------------------------------------------------------
-- mv_hr_report_review computes the two independently:
--
--     filing_lag_hours = round(extract(epoch from submitted_on - first_clock_in)
--                              / 3600.0, 1)              -- ROUNDED to 1dp
--     is_late_filing   = submitted_on >= first_clock_in + interval '49:00:00'
--                                                        -- EXACT instant
--
-- hr_review_summary's trend buckets keyed off the ROUNDED value
-- (filing_lag_hours >= 49), so a report filed at 48.96h rounds to 49.0 and was
-- counted in the red band while is_late_filing correctly said on time. Two such
-- rows exist in 2026-07-14..2026-08-05 (emp 251109 on 07-20, emp 241001 on
-- 07-28, both showing exactly 49.0).
--
-- is_late_filing is authoritative: HR's rule is "filed 49 hours or more after
-- time-in", measured on the real instant, and it is what the late KPI, the late
-- badge, the `late` chip and LateMissingTrend already use. The rounded column
-- stays as the DISPLAY value for the Filing lag column; it just stops being a
-- classifier. After this migration, the chart's red band, the `late` flag, and
-- the KPI cannot disagree, so drilling into a bar always reproduces its number.
--
-- Net effect on numbers: the red band loses the [48.95, 49.0) rows it should
-- never have held (2 rows across the 23 days sampled) and the on-time band
-- gains them. Total filed per day is unchanged.
--
-- ---------------------------------------------------------------------------
-- Changes
-- ---------------------------------------------------------------------------
--   1. hr_review_page  + hr_review_count: new `on_time` flag
--        = filed AND NOT is_late_filing
--      `late` is unchanged, so the union `on_time,late` (flags are OR-ed) is
--      exactly the bar's filed population. No separate `filed` flag needed.
--   2. hr_review_summary: trend buckets classify on is_late_filing instead of
--      the rounded filing_lag_hours. lag_le24 keeps its rounded < 24 test (a
--      display bucket well away from the boundary; it is only ever compared
--      against the other on-time bucket).
--
-- All three are CREATE OR REPLACE with unchanged signatures. Verified before
-- writing: zero dependent objects on any of them (pg_depend, deptype <> 'n'),
-- so nothing needs dropping and no rowtype cascade is in play.
--
-- Rollback: at the bottom of this file.

begin;

-- ---------------------------------------------------------------------------
-- 1. hr_review_page: add the `on_time` flag
-- ---------------------------------------------------------------------------
create or replace function analytics.hr_review_page(
  p_search text default null, p_carrier_group text default null,
  p_status text default null, p_date_from date default null,
  p_date_to date default null, p_flags text[] default '{}'::text[],
  p_sort_key text default null, p_sort_dir text default 'desc',
  p_offset integer default 0, p_limit integer default 100,
  p_carrier_groups text[] default null, p_statuses text[] default null,
  p_dows integer[] default null, p_untimed_min integer default null,
  p_untimed_max integer default null, p_stated_min numeric default null,
  p_stated_max numeric default null, p_positions text[] default null
) returns setof analytics.v_hr_report_review
language sql stable
set search_path to 'analytics', 'data_staging', 'reference'
as $function$
  select v.*
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_positions is null or v."position" = any(p_positions))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (p_stated_min is null or (v.stated_hours_net is not null and v.stated_hours_net >= p_stated_min))
    and (p_stated_max is null or (v.stated_hours_net is not null and v.stated_hours_net <= p_stated_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      -- The on-time half of the filing-speed bar: filed, and not late by the
      -- authoritative instant test. Mirrors lag_le24 + lag_24_48 below.
      or ('on_time'      = any(p_flags) and v.is_matured and v.has_time_in
                                        and v.submitted_on_et is not null
                                        and v.task_status <> 'cancelled'
                                        and not v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'asc'  then v.clock_in_et   end asc  nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'desc' then v.clock_in_et   end desc nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'asc'  then v.first_task_start end asc  nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'desc' then v.first_task_start end desc nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'asc'  then v.last_task_end    end asc  nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'desc' then v.last_task_end    end desc nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'asc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end asc  nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'desc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end desc nulls last,
    case when p_sort_dir = 'asc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end desc nulls last,
    case when p_sort_key = 'work_date' and p_sort_dir = 'asc' then v.work_date end asc,
    case when p_sort_key is distinct from 'work_date' or p_sort_dir <> 'asc' then v.work_date end desc,
    v.task_did asc
  offset greatest(coalesce(p_offset, 0), 0)
  limit  least(greatest(coalesce(p_limit, 100), 0), 1000)
$function$;

-- ---------------------------------------------------------------------------
-- 2. hr_review_count: same flag, same predicate (must stay byte-identical in
--    meaning or the header count disagrees with the rows below it)
-- ---------------------------------------------------------------------------
create or replace function analytics.hr_review_count(
  p_search text default null, p_carrier_group text default null,
  p_status text default null, p_date_from date default null,
  p_date_to date default null, p_flags text[] default '{}'::text[],
  p_carrier_groups text[] default null, p_statuses text[] default null,
  p_dows integer[] default null, p_untimed_min integer default null,
  p_untimed_max integer default null, p_stated_min numeric default null,
  p_stated_max numeric default null, p_positions text[] default null
) returns bigint
language sql stable
set search_path to 'analytics', 'data_staging', 'reference'
as $function$
  select count(*)
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_positions is null or v."position" = any(p_positions))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (p_stated_min is null or (v.stated_hours_net is not null and v.stated_hours_net >= p_stated_min))
    and (p_stated_max is null or (v.stated_hours_net is not null and v.stated_hours_net <= p_stated_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('on_time'      = any(p_flags) and v.is_matured and v.has_time_in
                                        and v.submitted_on_et is not null
                                        and v.task_status <> 'cancelled'
                                        and not v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
$function$;

-- ---------------------------------------------------------------------------
-- 3. hr_review_summary: classify the trend buckets on is_late_filing
-- ---------------------------------------------------------------------------
-- Only the two upper buckets change:
--   lag_24_48  ... filing_lag_hours >= 24 and filing_lag_hours < 49
--           ->  ... filing_lag_hours >= 24 and not is_late_filing
--   lag_over48 ... filing_lag_hours >= 49
--           ->  ... is_late_filing
-- lag_le24 is untouched. The three buckets still sum to filed_n exactly:
-- every filed row has a non-null filing_lag_hours (has_time_in implies
-- first_clock_in is not null and submitted_on_et is not null), so it lands in
-- exactly one of < 24 / (>= 24 and not late) / late.
create or replace function analytics.hr_review_summary(p_from date, p_to date)
returns jsonb
language sql stable
set search_path to 'analytics', 'data_staging', 'reference'
as $function$
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
               and task_status <> 'cancelled' and filing_lag_hours >= 24
               and not is_late_filing
           ) as lag_24_48,
           count(*) filter (
             where is_matured and has_time_in and submitted_on_et is not null
               and task_status <> 'cancelled' and is_late_filing
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

commit;

-- ---------------------------------------------------------------------------
-- Verification (run after apply; expected values from 2026-08-07)
-- ---------------------------------------------------------------------------
-- A. Every bucket reconciles with the drill-down flags, every day:
--      select t.d, t.filed_n, t.lag_le24 + t.lag_24_48 as green, t.lag_over48 as red
--      from jsonb_to_recordset(
--             (analytics.hr_review_summary(date '2026-07-14', date '2026-08-05'))->'trend'
--           ) as t(d date, filed_n int, lag_le24 int, lag_24_48 int, lag_over48 int)
--      where t.lag_le24 + t.lag_24_48 + t.lag_over48 <> t.filed_n;
--    -> expect 0 rows (buckets partition the filed set)
--
-- B. Flag counts equal the bar segments for 2026-08-01:
--      analytics.hr_review_count(p_date_from => '2026-08-01', p_date_to => '2026-08-01',
--                                p_flags => array['on_time'])        -> 14 - late
--      ... p_flags => array['late']                                   -> red
--      ... p_flags => array['on_time','late']                         -> 14
--
-- C. The boundary rows moved from red to green (not lost):
--      select count(*) from analytics.v_hr_report_review
--      where work_date between '2026-07-14' and '2026-08-05'
--        and filing_lag_hours = 49.0 and not is_late_filing;
--    -> 2 rows; these now count on-time in the chart, matching the `late` flag.
--
-- ---------------------------------------------------------------------------
-- Rollback
-- ---------------------------------------------------------------------------
-- Restores all three prior bodies. Safe at any time: no signature change, no
-- dependent objects, and the app tolerates an unknown flag (the RPC simply
-- matches nothing for it, so an `on_time` deep link degrades to an empty list
-- rather than erroring).
--
--   begin;
--   -- hr_review_page / hr_review_count: drop the two `on_time` OR-branches
--   --   added above; every other line is unchanged from 199/221.
--   -- hr_review_summary: restore the rounded tests
--   --   lag_24_48  -> filing_lag_hours >= 24 and filing_lag_hours < 49
--   --   lag_over48 -> filing_lag_hours >= 49
--   commit;
