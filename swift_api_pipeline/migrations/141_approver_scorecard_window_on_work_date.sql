-- 141: window EVERY date-sensitive metric in analytics.approver_scorecard on the
-- WORK DATE (a.work_date), not the submitted/approval date.
--
-- Before this migration the filter was split across dimensions: pending /
-- pending_employees / cohort_approved windowed on submitted_on_et, while the
-- approval-performance metrics (approved / on-time / amber / late / avg latency)
-- windowed on approved_on_et. That made the scorecard's date filter mean two
-- different things at once and diverge from the Browse tab, which already filters
-- on work_date. Now the range means one thing everywhere: "reports whose WORK DATE
-- falls in [p_from, p_to]". Consistent with analytics query applyBrowseFilters.
--
-- work_date is a non-null date in v_daily_report_approvals (the view filters
-- work_date IS NOT NULL), so no cast and no rows silently dropped.
--
-- Note: with everything on work_date, cohort_approved (approved & in-window) and
-- approved_count (approved & in-window) are now numerically identical. Kept as
-- separate columns because the UI uses them in different places (cohort powers the
-- completion ratio in the drill-down panel; approved_count powers the table column
-- and the SLA distribution). Return type is unchanged from 138, so CREATE OR REPLACE.
-- SLA buckets 3/7 MUST stay in sync with ontel-people/lib/hr/domain/approval-sla.ts (canonical).

CREATE OR REPLACE FUNCTION analytics.approver_scorecard(
  p_from date DEFAULT NULL,
  p_to   date DEFAULT NULL
)
RETURNS TABLE (
  group_label       text,
  display_label     text,
  carrier_group     text,
  employees         integer,
  pending           integer,
  pending_employees integer,
  cohort_approved   integer,
  approvers         integer,
  approved_count    integer,
  on_time_count     integer,
  amber_count       integer,
  late_count        integer,
  avg_latency_days  numeric
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = analytics, reference, public
AS $$
  WITH scoped AS (
    SELECT
      COALESCE(a.assigned_approver, '(no approver assigned)') AS group_label,
      a.emp_id,
      a.is_awaiting_approval,
      a.task_status,
      a.approved_by,
      a.approval_latency_days,
      -- one window for everything: the report's WORK DATE
      (p_from IS NULL OR a.work_date >= p_from)
        AND (p_to IS NULL OR a.work_date <= p_to) AS in_range
    FROM analytics.v_daily_report_approvals a
  ),
  agg AS (
    SELECT
      group_label,
      count(*) FILTER (WHERE is_awaiting_approval AND in_range)                         AS pending,
      count(DISTINCT emp_id) FILTER (WHERE is_awaiting_approval AND in_range)           AS pending_employees,
      count(*) FILTER (WHERE task_status = 'approved' AND in_range)                     AS cohort_approved,
      count(DISTINCT approved_by) FILTER (WHERE task_status = 'approved' AND in_range)  AS approvers,
      count(*) FILTER (WHERE task_status = 'approved' AND in_range)                     AS approved_count,
      count(*) FILTER (WHERE task_status = 'approved' AND in_range
                                        AND approval_latency_days <= 3)                 AS on_time_count,
      count(*) FILTER (WHERE task_status = 'approved' AND in_range
                                        AND approval_latency_days > 3
                                        AND approval_latency_days <= 7)                 AS amber_count,
      count(*) FILTER (WHERE task_status = 'approved' AND in_range
                                        AND approval_latency_days > 7)                  AS late_count,
      round(avg(approval_latency_days) FILTER (WHERE task_status = 'approved' AND in_range)::numeric, 1) AS avg_latency_days
    FROM scoped
    GROUP BY group_label
  ),
  emp AS (
    SELECT carrier_group, count(*)::int AS n
    FROM reference.ref_employees
    WHERE is_active AND carrier_group IS NOT NULL
    GROUP BY carrier_group
  )
  SELECT
    agg.group_label,
    COALESCE(m.display_label, agg.group_label) AS display_label,
    m.carrier_group,
    emp.n                            AS employees,
    agg.pending::int,
    agg.pending_employees::int,
    agg.cohort_approved::int,
    agg.approvers::int,
    agg.approved_count::int,
    agg.on_time_count::int,
    agg.amber_count::int,
    agg.late_count::int,
    agg.avg_latency_days
  FROM agg
  LEFT JOIN reference.ref_approver_group m ON m.group_label = agg.group_label
  LEFT JOIN emp ON emp.carrier_group = m.carrier_group
  WHERE COALESCE(m.include, true)
  ORDER BY agg.avg_latency_days DESC NULLS LAST;
$$;

REVOKE ALL ON FUNCTION analytics.approver_scorecard(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_scorecard(date, date) TO service_role;

UPDATE agent.schema_metadata
SET description =
  'Function(p_from,p_to): group-grain daily-report approval turnaround. One row per approver group. employees is a current headcount snapshot; every other date-sensitive metric (pending, pending_employees, cohort_approved, approved/on-time/amber/late/avg latency) windows on the report WORK DATE. Params NULL = all time.'
WHERE schema_name = 'analytics' AND table_name = 'approver_scorecard';
