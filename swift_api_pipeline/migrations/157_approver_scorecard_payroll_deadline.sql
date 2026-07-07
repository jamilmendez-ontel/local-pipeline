-- 157: Re-base the approver scorecard on the PAYROLL-CUTOFF approval deadline
-- instead of days-from-submission SLA buckets.
--
-- Old model (migrations 134/141): a report was on-time if approved within 3 days
-- of submission, amber 4-7, late >7. That measured raw approver responsiveness but
-- not the thing HR actually cares about: did the report get approved before its
-- payroll cutoff.
--
-- New model (spec confirmed 2026-07-06, pairs with lib/hr/domain/approval-deadline.ts
-- in ontel-people -- KEEP THE TWO IN SYNC):
--   * A report's PAY PERIOD comes from its work_date: day <= 15 -> period ends the
--     15th; day >= 16 -> period ends the last day of the month.
--   * DEADLINE = period end + 2 WORKING days (Mon-Fri; PH holidays ignored for now).
--     Approving anytime within the work period OR the grace counts as on time.
--   * on_time  = approved on or before the deadline.
--   * late     = approved AFTER the deadline, OR still unapproved once the deadline
--                has passed (missed the cutoff).
--   * filed_late = the employee SUBMITTED after the deadline. The approver never had
--                a chance to be on time, so these are EXCLUDED from on_time/late and
--                surfaced as their own count.
--   * on-time rate (app) = on_time / (on_time + late).
--   * avg_latency_days stays as a secondary "responsiveness" figure, no longer the
--     status driver. The 3/7 amber bucket is retired.
--
-- Deadline arithmetic: closed-form dow offset, verified byte-equal to a brute-force
-- weekday counter (= the approval-deadline.ts loop) across all 72 cutoffs 2025-2027.
-- Dates compared on America/New_York, consistent with the scorecard's other latency
-- math (migration 143); the Report Review row column uses PHT and can differ only
-- for approvals in the ~12h window around a deadline midnight.
--
-- Return columns change (amber_count + cohort_approved dropped, filed_late_count
-- added), so this is a DROP + CREATE, not CREATE OR REPLACE.

DROP FUNCTION IF EXISTS analytics.approver_scorecard(date, date);

CREATE FUNCTION analytics.approver_scorecard(
  p_from date DEFAULT NULL,
  p_to   date DEFAULT NULL
)
RETURNS TABLE (
  group_label      text,
  display_label    text,
  carrier_group    text,
  employees        integer,
  pending          integer,
  pending_employees integer,
  approvers        integer,
  approved_count   integer,
  on_time_count    integer,
  late_count       integer,
  filed_late_count integer,
  avg_latency_days numeric
)
LANGUAGE sql
STABLE
SET search_path = analytics, reference, public
AS $$
  WITH scoped AS (
    SELECT
      COALESCE(a.assigned_approver, '(no approver assigned)') AS group_label,
      a.emp_id,
      a.is_awaiting_approval,
      a.approved_by,
      a.approval_latency_days,
      (a.task_status = 'approved')            AS approved,
      (a.approved_on_et)::date                AS approved_date,
      (a.submitted_on_et)::date               AS submitted_date,
      (p_from IS NULL OR a.work_date >= p_from)
        AND (p_to IS NULL OR a.work_date <= p_to) AS in_range,
      -- payroll deadline = period_end + 2 working days (closed-form dow offset)
      (pe + (CASE extract(dow FROM pe)::int
               WHEN 4 THEN 4   -- Thu -> next Mon
               WHEN 5 THEN 4   -- Fri -> next Tue
               WHEN 6 THEN 3   -- Sat -> next Tue
               ELSE 2 END))    AS deadline
    FROM analytics.v_daily_report_approvals a
    CROSS JOIN LATERAL (
      SELECT CASE WHEN extract(day FROM a.work_date) <= 15
                  THEN date_trunc('month', a.work_date)::date + 14
                  ELSE (date_trunc('month', a.work_date) + interval '1 month' - interval '1 day')::date
             END AS pe
    ) p
  ),
  cls AS (
    SELECT *,
      (submitted_date IS NOT NULL AND submitted_date > deadline)  AS filed_late,
      (now() AT TIME ZONE 'America/New_York')::date               AS today_et
    FROM scoped
  ),
  agg AS (
    SELECT
      group_label,
      count(*) FILTER (WHERE is_awaiting_approval AND in_range)               AS pending,
      count(DISTINCT emp_id) FILTER (WHERE is_awaiting_approval AND in_range) AS pending_employees,
      count(DISTINCT approved_by) FILTER (WHERE approved AND in_range)        AS approvers,
      count(*) FILTER (WHERE approved AND in_range)                          AS approved_count,
      -- on time: approved by the deadline (unknown approved-at treated as on time),
      -- excluding reports the employee filed after their own deadline.
      count(*) FILTER (WHERE in_range AND NOT filed_late AND approved
                             AND (approved_date IS NULL OR approved_date <= deadline)) AS on_time_count,
      -- late: approved after the deadline, or unapproved past the deadline (missed).
      count(*) FILTER (WHERE in_range AND NOT filed_late
                             AND ( (approved AND approved_date IS NOT NULL AND approved_date > deadline)
                                   OR (NOT approved AND is_awaiting_approval AND today_et > deadline) )) AS late_count,
      count(*) FILTER (WHERE in_range AND filed_late)                        AS filed_late_count,
      round(avg(approval_latency_days) FILTER (WHERE approved AND in_range)::numeric, 1) AS avg_latency_days
    FROM cls
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
    emp.n                       AS employees,
    agg.pending::int,
    agg.pending_employees::int,
    agg.approvers::int,
    agg.approved_count::int,
    agg.on_time_count::int,
    agg.late_count::int,
    agg.filed_late_count::int,
    agg.avg_latency_days
  FROM agg
  LEFT JOIN reference.ref_approver_group m ON m.group_label = agg.group_label
  LEFT JOIN emp ON emp.carrier_group = m.carrier_group
  WHERE COALESCE(m.include, true)   -- unseeded groups default to visible; only include=false hides
  ORDER BY agg.late_count DESC NULLS LAST, agg.avg_latency_days DESC NULLS LAST, agg.group_label;
$$;

REVOKE ALL ON FUNCTION analytics.approver_scorecard(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_scorecard(date, date) TO service_role;

-- Refresh the semantic-layer metadata row (unique is on schema+table+column, and
-- column_name is NULL for object-level rows, so DELETE-then-INSERT is the clean upsert).
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'approver_scorecard' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approver_scorecard',
   'Function(p_from,p_to): group-grain daily-report approval turnaround, PAYROLL-CUTOFF basis. One row per approver group with current pending + employees and, windowed on work_date, on_time / late / filed_late counts vs the payroll deadline (period end [15th or EOM] + 2 working days) plus secondary avg submit-to-approve latency. Params NULL = all time.',
   'Powers the HR approver scorecard / Approval Performance page: which approver group is missing payroll cutoffs. on_time = approved by the deadline; late = approved after it or unapproved past it; filed_late (employee submitted after the deadline) is excluded from on_time/late. Deadline logic pairs with ontel-people lib/hr/domain/approval-deadline.ts.',
   ARRAY['analytics.v_daily_report_approvals','reference.ref_approver_group','reference.ref_employees']);
