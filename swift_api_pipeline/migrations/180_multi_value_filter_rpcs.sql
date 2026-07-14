-- 180: multi-select Division/Status filters for Ontel DRMC (Jamil 2026-07-14).
-- hr_review_page / hr_review_count (DR Monitoring list+count) and
-- approval_queue_summary (approvals queue KPIs/backlog) gain APPENDED,
-- DEFAULTED array params: p_carrier_groups text[] and (review only)
-- p_statuses text[]. The old single-value params stay and still work, so the
-- already-deployed app keeps functioning while Vercel rolls the new build
-- (it sends only one style per call; the predicates AND together).
-- Old signatures are DROPped first: appending defaulted params otherwise
-- creates an overload PostgREST can't disambiguate.
-- Base definitions: live pg_get_functiondef as of 2026-07-14 (post-179).

DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer);
DROP FUNCTION IF EXISTS analytics.hr_review_count(text, text, text, date, date, text[]);
DROP FUNCTION IF EXISTS analytics.approval_queue_summary(text, text, text);

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
  p_statuses text[] DEFAULT NULL::text[]
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
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.variance_hours > 1 and v.coverage_pct < 90)
      or ('variance_red' = any(p_flags) and v.variance_hours > 2 and v.coverage_pct < 75)
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
        when 'stated_hours'     then v.stated_hours
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours
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

CREATE FUNCTION analytics.hr_review_count(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[]
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
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.variance_hours > 1 and v.coverage_pct < 90)
      or ('variance_red' = any(p_flags) and v.variance_hours > 2 and v.coverage_pct < 75)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
    )
$function$;

CREATE FUNCTION analytics.approval_queue_summary(
  p_carrier_group text DEFAULT NULL::text,
  p_division text DEFAULT NULL::text,
  p_search text DEFAULT NULL::text,
  p_carrier_groups text[] DEFAULT NULL::text[]
)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'public'
AS $function$
  with q as (
    select assigned_approver, no_approver_flag, pending_wait_days
    from analytics.v_daily_report_approvals
    where is_awaiting_approval = true
      and (p_carrier_group is null or carrier_group = p_carrier_group)
      and (p_carrier_groups is null or carrier_group = any(p_carrier_groups))
      and (p_division is null or division = p_division)
      and (p_search is null or employee_name ilike '%'||p_search||'%' or emp_id ilike '%'||p_search||'%')
  ),
  tagged as (
    select coalesce(assigned_approver, '(unassigned)') as grp,
           no_approver_flag,
           coalesce(pending_wait_days, 0) as w,
           case when coalesce(pending_wait_days,0) between 4 and 7 then 'amber'
                when coalesce(pending_wait_days,0) > 7 then 'red' else 'ok' end as bucket
    from q
  ),
  backlog as (
    select grp as "group",
           count(*) as waiting,
           count(*) filter (where bucket='amber') as amber,
           count(*) filter (where bucket='red') as red
    from tagged group by grp order by count(*) desc
  )
  select jsonb_build_object(
    'kpis', jsonb_build_object(
      'awaiting', (select count(*) from tagged),
      'amber',    (select count(*) from tagged where bucket='amber'),
      'red',      (select count(*) from tagged where bucket='red'),
      'oldest_wait_days', (select coalesce(max(w),0) from tagged),
      'no_approver', (select count(*) from tagged where no_approver_flag)
    ),
    'backlog', (select coalesce(jsonb_agg(jsonb_build_object(
                  'group', "group", 'waiting', waiting, 'amber', amber, 'red', red)
                  order by waiting desc), '[]'::jsonb) from backlog)
  );
$function$;

NOTIFY pgrst, 'reload schema';
