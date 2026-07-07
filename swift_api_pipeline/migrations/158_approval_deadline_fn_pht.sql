-- 158: Make the payroll-approval deadline a single canonical SQL function, and
-- decide on-time/late on the PHILIPPINE calendar (matching the Report Review column).
--
-- Two follow-ups to migration 157:
--
-- (1) SINGLE SOURCE OF TRUTH for the deadline. 157 inlined the "period end + 2
--     working days" arithmetic inside approver_scorecard. Promote it to
--     analytics.approval_deadline(work_date) so there is exactly one SQL definition
--     to change when the rule changes. It still pairs with the TS canonical
--     ontel-people/lib/hr/domain/approval-deadline.ts (a drift test asserts the two
--     agree), but on the SQL side nothing is duplicated anymore.
--
-- (2) COMPARE ON PHT, not ET. 157 compared the approval/submission date on
--     America/New_York, but the payroll cutoff is a Philippine concept and the
--     Report Review "Approval wait" column already decides on Asia/Manila (via
--     formatPhtDate: reverse the ET wall-clock to the true UTC instant, then take
--     the Manila calendar day). Because most approvals happen ET-evening = next PHT
--     day, the two calendars disagree for ~8,600 of the ~13,700 approvals, so this
--     is a real alignment, not a midnight edge case. The SQL mirrors formatPhtDate
--     exactly: (et_wall AT TIME ZONE 'America/New_York') AT TIME ZONE 'Asia/Manila'
--     -- verified 0 round-trip mismatches vs the raw UTC timestamp on prod.
--     avg_latency_days stays ET (a duration; migration 143 keeps latency on ET).
--
-- Return columns are unchanged from 157, so approver_scorecard is CREATE OR REPLACE.

-- 1) Canonical deadline: pay period end (15th or EOM) + 2 working days (skip
--    weekends; PH holidays ignored for now). Pure function of the date -> IMMUTABLE.
--    dow offset {Thu,Fri +4; Sat +3; else +2} verified byte-equal to a brute-force
--    weekday counter (= approval-deadline.ts) across all 72 cutoffs 2025-2027.
CREATE OR REPLACE FUNCTION analytics.approval_deadline(work_date date)
RETURNS date
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT pe + (CASE extract(dow FROM pe)::int
                 WHEN 4 THEN 4   -- Thu -> next Mon
                 WHEN 5 THEN 4   -- Fri -> next Tue
                 WHEN 6 THEN 3   -- Sat -> next Tue
                 ELSE 2 END)
  FROM (
    SELECT CASE WHEN extract(day FROM work_date) <= 15
                THEN date_trunc('month', work_date)::date + 14
                ELSE (date_trunc('month', work_date) + interval '1 month' - interval '1 day')::date
           END AS pe
  ) p;
$$;

REVOKE ALL ON FUNCTION analytics.approval_deadline(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approval_deadline(date) TO service_role;

-- 2) Recompute the scorecard using the canonical deadline + PHT comparisons.
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
      -- approval / submission date on the PHILIPPINE calendar (reverse the view's ET
      -- wall-clock to the true instant, then take the Manila day) -- mirrors formatPhtDate.
      ((a.approved_on_et  AT TIME ZONE 'America/New_York') AT TIME ZONE 'Asia/Manila')::date AS approved_date,
      ((a.submitted_on_et AT TIME ZONE 'America/New_York') AT TIME ZONE 'Asia/Manila')::date AS submitted_date,
      (p_from IS NULL OR a.work_date >= p_from)
        AND (p_to IS NULL OR a.work_date <= p_to) AS in_range,
      analytics.approval_deadline(a.work_date)  AS deadline
    FROM analytics.v_daily_report_approvals a
  ),
  cls AS (
    SELECT *,
      (submitted_date IS NOT NULL AND submitted_date > deadline)  AS filed_late,
      (now() AT TIME ZONE 'Asia/Manila')::date                    AS today_pht
    FROM scoped
  ),
  agg AS (
    SELECT
      group_label,
      count(*) FILTER (WHERE is_awaiting_approval AND in_range)               AS pending,
      count(DISTINCT emp_id) FILTER (WHERE is_awaiting_approval AND in_range) AS pending_employees,
      count(DISTINCT approved_by) FILTER (WHERE approved AND in_range)        AS approvers,
      count(*) FILTER (WHERE approved AND in_range)                          AS approved_count,
      count(*) FILTER (WHERE in_range AND NOT filed_late AND approved
                             AND (approved_date IS NULL OR approved_date <= deadline)) AS on_time_count,
      count(*) FILTER (WHERE in_range AND NOT filed_late
                             AND ( (approved AND approved_date IS NOT NULL AND approved_date > deadline)
                                   OR (NOT approved AND is_awaiting_approval AND today_pht > deadline) )) AS late_count,
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
  WHERE COALESCE(m.include, true)
  ORDER BY agg.late_count DESC NULLS LAST, agg.avg_latency_days DESC NULLS LAST, agg.group_label;
$$;

REVOKE ALL ON FUNCTION analytics.approver_scorecard(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_scorecard(date, date) TO service_role;

-- Semantic-layer metadata for the new function (object-level row: column_name NULL).
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'approval_deadline' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approval_deadline',
   'Function(work_date date) -> date: the payroll approval deadline for a daily report. Pay period end (work_date day <=15 -> the 15th, else the last day of the month) + 2 working days (weekends skipped; PH holidays ignored). IMMUTABLE.',
   'Single SQL source of truth for the approval-cutoff deadline, used by analytics.approver_scorecard and any other consumer. Mirrors ontel-people lib/hr/domain/approval-deadline.ts (a drift test keeps them equal).',
   ARRAY[]::text[]);

-- Make the new function visible to PostgREST immediately (for the drift test's rpc call).
NOTIFY pgrst, 'reload schema';
