-- 178: HR Dashboard approver-compliance RPC (replaces the raw-latency card).
--
-- The dashboard's "Approval latency per day" chart graded approvers on raw
-- submitted->approved speed, which the payroll-cutoff rule (approval_deadline,
-- migration 158) explicitly rejects: approvals batch bi-monthly, so raw daily
-- medians sawtooth AND carry survivorship bias (only approved reports have a
-- latency, so the chart looks best exactly when the backlog is worst).
--
-- One function returns both replacement payloads in one call (the app pays ~3
-- sequential auth round trips per action, so two RPCs would double that):
--   periods  - the last 7 pay periods (15th/EOM grain, the rule's native grain),
--              each graded with EXACTLY migration 158's classification: PHT
--              calendar days, filed-late (employee submitted after the deadline)
--              excluded from on-time/late, unapproved past deadline = late now.
--              The in-flight period (deadline still ahead) reports pending_not_due
--              so the card can render it hatched/ungraded instead of fake-100%.
--   overdue  - as-of-now aging of awaiting reports past their deadline (calendar
--              days overdue, BacklogAging-style buckets), with filed-late-pending
--              counted separately (not graded, but still in someone's queue).
--   countdown - the period whose deadline is next (the just-ended period during
--              its grace window, else the current one) + its pending count.
CREATE OR REPLACE FUNCTION analytics.hr_approval_compliance()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = analytics, reference, public
AS $$
WITH cur AS (
  SELECT (now() AT TIME ZONE 'Asia/Manila')::date AS today_pht,
         CASE WHEN extract(day FROM (now() AT TIME ZONE 'Asia/Manila')::date)::int <= 15
              THEN date_trunc('month', (now() AT TIME ZONE 'Asia/Manila')::date)::date + 14
              ELSE (date_trunc('month', (now() AT TIME ZONE 'Asia/Manila')::date) + interval '1 month' - interval '1 day')::date
         END AS cur_pe
),
cls AS (
  SELECT
    CASE WHEN extract(day FROM a.work_date)::int <= 15
         THEN date_trunc('month', a.work_date)::date + 14
         ELSE (date_trunc('month', a.work_date) + interval '1 month' - interval '1 day')::date
    END AS period_end,
    a.is_awaiting_approval,
    (a.task_status = 'approved') AS approved,
    -- PHT calendar day, mirroring formatPhtDate (see migration 158)
    ((a.approved_on_et  AT TIME ZONE 'America/New_York') AT TIME ZONE 'Asia/Manila')::date AS approved_date,
    ((a.submitted_on_et AT TIME ZONE 'America/New_York') AT TIME ZONE 'Asia/Manila')::date AS submitted_date,
    analytics.approval_deadline(a.work_date) AS deadline,
    cur.today_pht, cur.cur_pe
  FROM analytics.v_daily_report_approvals a
  -- Same group scoping as approver_scorecard: groups marked include=false in
  -- ref_approver_group are out of grading scope, so the card reconciles
  -- exactly with /approvals/scorecard.
  LEFT JOIN reference.ref_approver_group m
    ON m.group_label = COALESCE(a.assigned_approver, '(no approver assigned)')
  CROSS JOIN cur
  WHERE COALESCE(m.include, true)
),
cls2 AS (
  SELECT c.*, (submitted_date IS NOT NULL AND submitted_date > deadline) AS filed_late
  FROM cls c
),
period_agg AS (
  SELECT period_end, max(deadline) AS deadline,
    count(*) FILTER (WHERE NOT filed_late AND approved
                       AND (approved_date IS NULL OR approved_date <= deadline)) AS on_time,
    count(*) FILTER (WHERE NOT filed_late
                       AND ( (approved AND approved_date IS NOT NULL AND approved_date > deadline)
                             OR (NOT approved AND is_awaiting_approval AND today_pht > deadline) )) AS late,
    count(*) FILTER (WHERE filed_late) AS filed_late_n,
    count(*) FILTER (WHERE is_awaiting_approval AND today_pht <= deadline) AS pending_not_due
  FROM cls2
  WHERE period_end <= cur_pe
  GROUP BY period_end
  ORDER BY period_end DESC
  LIMIT 7
),
overdue_agg AS (
  SELECT
    count(*) FILTER (WHERE NOT filed_late) AS total,
    max(today_pht - deadline) FILTER (WHERE NOT filed_late) AS oldest_days,
    count(*) FILTER (WHERE NOT filed_late AND today_pht - deadline BETWEEN 1 AND 2)  AS b1_2,
    count(*) FILTER (WHERE NOT filed_late AND today_pht - deadline BETWEEN 3 AND 5)  AS b3_5,
    count(*) FILTER (WHERE NOT filed_late AND today_pht - deadline BETWEEN 6 AND 10) AS b6_10,
    count(*) FILTER (WHERE NOT filed_late AND today_pht - deadline >= 11)            AS b11p,
    count(*) FILTER (WHERE filed_late) AS filed_late_pending
  FROM cls2
  WHERE is_awaiting_approval AND deadline < today_pht
),
countdown AS (
  SELECT period_end, deadline, count(*) FILTER (WHERE is_awaiting_approval) AS pending
  FROM cls2
  WHERE deadline >= today_pht AND period_end <= cur_pe
  GROUP BY period_end, deadline
  ORDER BY deadline ASC
  LIMIT 1
)
SELECT jsonb_build_object(
  'today_pht', (SELECT today_pht FROM cur),
  'periods', (SELECT coalesce(jsonb_agg(jsonb_build_object(
        'period_end', period_end, 'deadline', deadline,
        'on_time', on_time, 'late', late, 'filed_late', filed_late_n,
        'pending_not_due', pending_not_due) ORDER BY period_end), '[]'::jsonb)
      FROM period_agg),
  'overdue', (SELECT jsonb_build_object(
        'total', coalesce(total, 0), 'oldest_days', oldest_days,
        'b1_2', coalesce(b1_2, 0), 'b3_5', coalesce(b3_5, 0),
        'b6_10', coalesce(b6_10, 0), 'b11p', coalesce(b11p, 0),
        'filed_late_pending', coalesce(filed_late_pending, 0))
      FROM overdue_agg),
  'countdown', (SELECT jsonb_build_object(
        'period_end', period_end, 'deadline', deadline, 'pending', pending)
      FROM countdown)
);
$$;

REVOKE ALL ON FUNCTION analytics.hr_approval_compliance() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.hr_approval_compliance() TO service_role;
