-- 190: net-of-break stated hours for Ontel DRMC variance (Jamil 2026-07-27).
-- A stated day of 5h or more includes a 1-hour unpaid break, so the DR
-- Monitoring "Stated hrs" column and all variance math should net that hour
-- out (days under 5h have no break, so they pass through unchanged). The
-- report detail drawer keeps the FULL stated hours (raw), so this must not
-- change stated_hours itself -- it adds stated_hours_net alongside and points
-- variance/coverage at the net value.
--
-- v_hr_report_review changes (CREATE OR REPLACE contract: existing column
-- names/types/order preserved byte-for-byte; only the variance_hours and
-- coverage_pct EXPRESSIONS change, and stated_hours_net is appended last):
--   * stated_hours       -- UNCHANGED, raw b.total_hours (drawer reads this).
--   * variance_hours     -- now round(stated_net - timed, 1).
--   * coverage_pct       -- now round(100 * timed / stated_net, 0), stated_net > 0.
--   * work_dow           -- UNCHANGED (migration 189).
--   * stated_hours_net   -- NEW, appended: total_hours - 1 when >= 5, else total_hours.
--
-- Because hr_review_page/count, the flag-chip counts, the variance KPI tile,
-- and the variance sort all read variance_hours/coverage_pct FROM this view,
-- they inherit the net-of-break math with no further change -- the on-screen
-- tier, the "High variance" count, the variance filter, and the sort all agree.
-- hr_review_page is recreated only to (a) clear the SETOF rowtype dependency so
-- the view can gain a column and (b) point the stated_hours sort at the net
-- column so the displayed column sorts by what it shows. hr_review_count is
-- unaffected (returns bigint, reads the net variance automatically).
--
-- Base definitions: live pg_get_functiondef / pg_get_viewdef as of 2026-07-27
-- (post-189). Rollback: re-create the view without stated_hours_net and with
-- variance_hours/coverage_pct computed from b.total_hours, and hr_review_page
-- with the stated_hours sort on v.stated_hours.

DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[]);

CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
 SELECT b.emp_id,
    b.employee_name,
    b.email,
    b."position",
    b.carrier_group,
    b.division,
    b.work_date,
    b.task_did,
    b.task_status,
    b.submitted_on_et,
    b.approved_on_et,
    b.clock_in_et,
    b.approval_latency_days,
    b.total_hours AS stated_hours,
    r.first_clock_in IS NOT NULL AS has_time_in,
    round(EXTRACT(epoch FROM t.submitted_on - r.first_clock_in) / 3600.0, 1) AS filing_lag_hours,
    r.first_clock_in + '49:00:00'::interval AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL AND t.submitted_on >= (r.first_clock_in + '49:00:00'::interval) AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_matured,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    COALESCE(tm.entry_count, 0::bigint) AS timer_entry_count,
    th.person_key IS NOT NULL AS has_timer_history,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL
              THEN round((CASE WHEN b.total_hours >= 5::numeric THEN b.total_hours - 1::numeric ELSE b.total_hours END) - tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL
              AND (CASE WHEN b.total_hours >= 5::numeric THEN b.total_hours - 1::numeric ELSE b.total_hours END) > 0::numeric
              THEN round(100.0 * tm.union_min / 60.0 / (CASE WHEN b.total_hours >= 5::numeric THEN b.total_hours - 1::numeric ELSE b.total_hours END), 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.shift_time_in_pht,
    b.clock_in_late_minutes,
    EXTRACT(dow FROM b.work_date)::smallint AS work_dow,
        CASE
            WHEN b.total_hours >= 5::numeric THEN b.total_hours - 1::numeric
            ELSE b.total_hours
        END AS stated_hours_net
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true;

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

NOTIFY pgrst, 'reload schema';
