-- 135: make analytics.approver_scorecard's `pending` respond to the date range.
-- Previously `pending` was always a current snapshot (all awaiting reports). Now, when
-- p_from/p_to are given, `pending` counts awaiting reports whose SUBMITTED date falls in
-- the range (pending reports have no approval date, so submitted_on_et is the only sensible
-- dimension). With NULL params it still returns the full current backlog, unchanged.
-- `employees` stays a current snapshot (headcount has no time dimension).
-- SLA buckets 3 / 7 MUST stay in sync with hr-system/lib/hr/approval-sla.ts (canonical).

CREATE OR REPLACE FUNCTION analytics.approver_scorecard(
  p_from date DEFAULT NULL,
  p_to   date DEFAULT NULL
)
RETURNS TABLE (
  group_label      text,
  display_label    text,
  carrier_group    text,
  employees        integer,
  pending          integer,
  approvers        integer,
  approved_count   integer,
  on_time_count    integer,
  amber_count      integer,
  late_count       integer,
  avg_latency_days numeric
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = analytics, reference, public
AS $$
  WITH scoped AS (
    SELECT
      COALESCE(a.assigned_approver, '(no approver assigned)') AS group_label,
      a.is_awaiting_approval,
      a.task_status,
      a.approved_by,
      a.approval_latency_days,
      -- approved metrics window: on approval date
      (a.task_status = 'approved'
        AND (p_from IS NULL OR a.approved_on_et::date >= p_from)
        AND (p_to   IS NULL OR a.approved_on_et::date <= p_to)) AS in_window,
      -- pending window: on submitted date (pending has no approval date)
      (a.is_awaiting_approval
        AND (p_from IS NULL OR a.submitted_on_et::date >= p_from)
        AND (p_to   IS NULL OR a.submitted_on_et::date <= p_to)) AS pending_in_window
    FROM analytics.v_daily_report_approvals a
  ),
  agg AS (
    SELECT
      group_label,
      count(*) FILTER (WHERE pending_in_window)                                        AS pending,
      count(DISTINCT approved_by) FILTER (WHERE in_window)                             AS approvers,
      count(*) FILTER (WHERE in_window)                                                AS approved_count,
      count(*) FILTER (WHERE in_window AND approval_latency_days <= 3)                 AS on_time_count,
      count(*) FILTER (WHERE in_window AND approval_latency_days > 3
                                        AND approval_latency_days <= 7)                AS amber_count,
      count(*) FILTER (WHERE in_window AND approval_latency_days > 7)                  AS late_count,
      round(avg(approval_latency_days) FILTER (WHERE in_window)::numeric, 1)           AS avg_latency_days
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
    emp.n                                      AS employees,
    agg.pending::int,
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
