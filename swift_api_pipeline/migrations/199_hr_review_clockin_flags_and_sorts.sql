-- 199: Excel-style column-header filters, Phase 1 (DR Monitoring). Jamil 2026-07-28.
-- Adds to the two DR Monitoring list RPCs, WITHOUT changing their signatures
-- (new p_flags values + new p_sort_key values are body-only):
--   * two clock-in flags:
--       'tardy'       -> clock_in_late_minutes > 30 (matches LATE_GRACE_MINUTES,
--                        warehouse migration 163 floors seconds to whole minutes)
--       'no_clock_in' -> clock_in_et is null  (broader than 'no_time_in', which is
--                        "submitted AND no clock-in"; both kept intentionally)
--   * two sort keys:
--       'clock_in_et'   -> first clock-in timestamp, nulls last
--       'approval_wait' -> approval delay magnitude via the new helper below
-- Also creates analytics.approval_wait_days(...), the SQL side of the app's
-- approvalWaitDays() (ontel-people lib/hr/domain/approval-wait-days.ts); the two
-- are drift-guarded by approval-wait-days.sql-sync.test.ts. Reuses the existing
-- analytics.approval_deadline() (migration 158) so the deadline rule stays single-sourced.
--
-- The signature is UNCHANGED from migration 198, so no overload hazard; we still
-- DROP+CREATE to replace the bodies cleanly (CREATE OR REPLACE cannot change a
-- SQL function's body if the return type/columns were to shift, and DROP keeps
-- the diff explicit). The retired 'variance'/'variance_red' predicates are left
-- in place (dead; the app no longer sends them), same as migration 198.
--
-- Base bodies: migration 198 verbatim, plus the two flag predicates, the two
-- ORDER BY branches, and the helper. Rollback: DROP approval_wait_days and re-run 198.

-- Helper: approval delay magnitude (null = "none"). Mirrors approvalWaitDays() in TS.
-- Naive-ET timestamps (v.*_on_et are `timestamp` = ET wall clock, warehouse
-- migration 162) are reinterpreted to PHT before taking the calendar date.
CREATE OR REPLACE FUNCTION analytics.approval_wait_days(
  p_work_date date,
  p_task_status text,
  p_submitted_on_et timestamp,
  p_approved_on_et timestamp
)
 RETURNS integer
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select case
    -- approved: daysLate = greatest(0, approved PHT date - deadline); unknown
    -- approved-at (approved by status only) -> 0, matching the TS branch.
    when p_task_status ilike '%approv%' or p_approved_on_et is not null then
      greatest(0, coalesce(
        ((p_approved_on_et at time zone 'America/New_York' at time zone 'Asia/Manila')::date
          - analytics.approval_deadline(p_work_date)), 0))
    -- awaiting/overdue: submitted and nobody has acted yet.
    when p_submitted_on_et is not null
         and (p_task_status ilike '%submit%' or p_task_status ilike '%pending%' or p_task_status ilike '%review%') then
      ((now() at time zone 'Asia/Manila')::date - analytics.approval_deadline(p_work_date))
    else null
  end
$function$;

DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[], integer, integer);
DROP FUNCTION IF EXISTS analytics.hr_review_count(text, text, text, date, date, text[], text[], text[], integer[], integer, integer);

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
  p_untimed_max integer DEFAULT NULL::integer
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
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.clock_in_late_minutes > 30)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'asc'  then v.clock_in_et   end asc  nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'desc' then v.clock_in_et   end desc nulls last,
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
  p_untimed_max integer DEFAULT NULL::integer
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
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.clock_in_late_minutes > 30)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
    )
$function$;

NOTIFY pgrst, 'reload schema';
