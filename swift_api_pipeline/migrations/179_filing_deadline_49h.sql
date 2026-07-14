-- 179: filing lateness boundary moves from >48h to >=49h (Jamil 2026-07-14).
-- HR rule: any lag within the 48th hour (48:00:00 through 48:59:59) still
-- counts as "48 hours" and is on time; a report is LATE the moment the lag
-- reaches 49:00:00. The same boundary drives deadline_at, is_missing_report
-- and is_matured (one filing-deadline concept), so all four expressions in
-- analytics.v_hr_report_review shift together, plus the two RPCs that
-- hardcode the old 48h: hr_review_summary (filing-speed buckets; JSON keys
-- lag_24_48 / lag_over48 KEPT for the app contract, only the boundary moves
-- so the red bucket still equals "late") and hr_review_backlog (overdue-days
-- anchor). hr_infraction_* and hr_review_page/count read is_late_filing from
-- the view and need no change. App-side twin: ontel-people
-- lib/hr/domain/missing-days.ts DEADLINE_WINDOW_MS (48h -> 49h, same change).
--
-- Base definition: live pg_get_viewdef as of 2026-07-14 (146 + late-badge
-- columns from 162/163 + person_key timer joins from 170). Columns unchanged
-- in name/order/type, so CREATE OR REPLACE is safe.

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
    r.first_clock_in + interval '49 hours' AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL
      AND t.submitted_on >= (r.first_clock_in + interval '49 hours') AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text]))
      AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL
      AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + interval '49 hours') AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL
      AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + interval '49 hours') AS is_matured,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    COALESCE(tm.entry_count, 0::bigint) AS timer_entry_count,
    th.person_key IS NOT NULL AS has_timer_history,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL THEN round(b.total_hours - tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric THEN round(100.0 * tm.union_min / 60.0 / b.total_hours, 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.shift_time_in_pht,
    b.clock_in_late_minutes
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true;

-- hr_review_summary: filing-speed buckets follow the new boundary so the red
-- bucket (lag_over48 key) still means "late": mid = 24 <= lag < 49, red = lag
-- >= 49. Everything else identical to the live definition (migration 152 line).
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

-- hr_review_backlog: overdue-days anchor follows the same 49h deadline.
CREATE OR REPLACE FUNCTION analytics.hr_review_backlog()
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  with m as (
    select extract(epoch from (now() - (evidence_at + interval '49 hours'))) / 86400.0 as overdue_days
    from analytics.v_hr_report_review
    where is_missing_report
  ), b as (
    select
      count(*)                                                        as total,
      round(max(overdue_days)::numeric, 1)                            as oldest_days,
      count(*) filter (where overdue_days < 2)                        as d_le2,
      count(*) filter (where overdue_days >= 2  and overdue_days < 7) as d_2_7,
      count(*) filter (where overdue_days >= 7  and overdue_days < 14) as d_7_14,
      count(*) filter (where overdue_days >= 14 and overdue_days < 30) as d_14_30,
      count(*) filter (where overdue_days >= 30)                      as d_30p
    from m
  )
  select jsonb_build_object(
    'total', (select total from b),
    'oldest_days', (select oldest_days from b),
    'buckets', jsonb_build_array(
      jsonb_build_object('label', '≤2d',    'n', (select d_le2 from b)),
      jsonb_build_object('label', '2–7d',   'n', (select d_2_7 from b)),
      jsonb_build_object('label', '7–14d',  'n', (select d_7_14 from b)),
      jsonb_build_object('label', '14–30d', 'n', (select d_14_30 from b)),
      jsonb_build_object('label', '30d+',   'n', (select d_30p from b))
    )
  );
$function$;

-- Semantic-layer metadata: reflect the new boundary.
UPDATE agent.schema_metadata
SET description = replace(replace(description,
      'filing lag vs 48h deadline', 'filing lag vs the 49h lateness boundary (48h + the full 48th hour counts as on time)'),
      '>48h', '>=49h')
WHERE schema_name = 'analytics'
  AND table_name IN ('v_hr_report_review', 'hr_review_summary', 'hr_review_backlog', 'hr_infraction_months', 'hr_infraction_detail');
