-- 227: revenue range filter on analytics.hr_review_page / hr_review_count
--
-- WHY: DR Monitoring's revenue column had no header filter, unlike Variance %
-- (p_untimed_min/max, migration 198) and Stated hrs (p_stated_min/max,
-- migration 219). Revenue was merged app-side AFTER the RPC page fetch, so
-- there was nothing for a filter to bite on: filtering client-side would only
-- filter the rows already loaded, which is wrong under infinite scroll and
-- wrong in the row count.
--
-- SHAPE: these functions still RETURN no revenue. They only FILTER on it. That
-- is deliberate and load-bearing for security: `hr_review_page` RETURNS SETOF
-- analytics.v_hr_report_review, so emitting revenue would mean widening that
-- core view, and it would put revenue figures in the payload of every caller
-- including viewers below the app's REVENUE_MIN_ROLE. Keeping the RPC
-- filter-only preserves the property that below-threshold viewers never
-- RECEIVE revenue data; the app still merges values for display, and only for
-- viewers it has already gated. (Sorting is still not offered: an ORDER BY on
-- revenue evaluates the correlated aggregate over the whole filtered set rather
-- than the returned page, which is a different performance question.)
--
-- SEMANTICS: a bound drops rows with no priced revenue for that member-day.
-- That matches the established convention on this table (p_stated_min/max drop
-- rows with no stated hours, p_untimed_min/max drop rows with no coverage), and
-- it is what "show me days worth $200+" means.
--
-- PERFORMANCE: the correlated EXISTS aggregates mv_timer_revenue_daily on
-- exactly its (user_email, work_date) index, so each probe is an index lookup
-- over a handful of task rows. Measured on live data 2026-08-07 over a 2-month
-- window (7,442 candidate rows): 162ms. Routing the same predicate through the
-- aggregating view analytics.v_timer_revenue_member_day instead cost 1,119ms,
-- because the date range could not be pushed into the grouped subquery and it
-- degenerated into a seq scan + HashAggregate of all 134,287 rows. Hence the
-- direct correlation here, and the functional index below so the match is
-- case-proof without giving up the index.
--
-- CREATE OR REPLACE cannot change a function's argument list (it would create
-- an ambiguous overload instead, and with every parameter defaulted the app's
-- named calls would fail with "function is not unique"). So each function is
-- dropped and recreated; the migration is one transaction, so no caller ever
-- sees a missing function.

-- Case-proof + index-backed correlation key. mv_timer_revenue_daily currently
-- holds 0 mixed-case emails, but a silent case drift would silently drop rows
-- from a FILTER, so match on lower() and index lower().
CREATE INDEX IF NOT EXISTS mv_timer_revenue_daily_lower_email_date_idx
    ON analytics.mv_timer_revenue_daily (lower(user_email), work_date);

DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[], integer, integer, numeric, numeric, text[]);

CREATE FUNCTION analytics.hr_review_page(
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
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer,
  p_stated_min numeric DEFAULT NULL::numeric,
  p_stated_max numeric DEFAULT NULL::numeric,
  p_positions text[] DEFAULT NULL::text[],
  p_rev_min numeric DEFAULT NULL::numeric,
  p_rev_max numeric DEFAULT NULL::numeric
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
    -- Revenue range (migration 227). Unset bounds = predicate absent, so the
    -- correlated probe never runs on the common path.
    and (
      (p_rev_min is null and p_rev_max is null)
      or (v.email is not null and exists (
            select 1
            from analytics.mv_timer_revenue_daily d
            where lower(d.user_email) = lower(v.email)
              and d.work_date = v.work_date
            group by lower(d.user_email), d.work_date
            having (p_rev_min is null
                    or coalesce(sum(d.amount_usd) filter (where d.pricing_status = 'priced'), 0) >= p_rev_min)
               and (p_rev_max is null
                    or coalesce(sum(d.amount_usd) filter (where d.pricing_status = 'priced'), 0) <= p_rev_max)
          ))
    )
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

DROP FUNCTION IF EXISTS analytics.hr_review_count(text, text, text, date, date, text[], text[], text[], integer[], integer, integer, numeric, numeric, text[]);

CREATE FUNCTION analytics.hr_review_count(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer,
  p_stated_min numeric DEFAULT NULL::numeric,
  p_stated_max numeric DEFAULT NULL::numeric,
  p_positions text[] DEFAULT NULL::text[],
  p_rev_min numeric DEFAULT NULL::numeric,
  p_rev_max numeric DEFAULT NULL::numeric
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
    -- Must stay IDENTICAL to hr_review_page's revenue predicate or the count
    -- disagrees with the list.
    and (
      (p_rev_min is null and p_rev_max is null)
      or (v.email is not null and exists (
            select 1
            from analytics.mv_timer_revenue_daily d
            where lower(d.user_email) = lower(v.email)
              and d.work_date = v.work_date
            group by lower(d.user_email), d.work_date
            having (p_rev_min is null
                    or coalesce(sum(d.amount_usd) filter (where d.pricing_status = 'priced'), 0) >= p_rev_min)
               and (p_rev_max is null
                    or coalesce(sum(d.amount_usd) filter (where d.pricing_status = 'priced'), 0) <= p_rev_max)
          ))
    )
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

-- Both functions are reached by the app's service role, same as before the drop.
GRANT EXECUTE ON FUNCTION analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[], integer, integer, numeric, numeric, text[], numeric, numeric) TO service_role;
GRANT EXECUTE ON FUNCTION analytics.hr_review_count(text, text, text, date, date, text[], text[], text[], integer[], integer, integer, numeric, numeric, text[], numeric, numeric) TO service_role;

-- New parameters change the PostgREST signature; make it pick them up now
-- rather than on the next unrelated DDL.
NOTIFY pgrst, 'reload schema';
