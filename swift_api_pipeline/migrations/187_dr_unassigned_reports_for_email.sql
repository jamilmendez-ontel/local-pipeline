-- 187: analytics.dr_unassigned_reports_for_email(p_email, p_all) -- the submitted
-- daily reports with NO approver assigned that belong to a given approver's
-- HR-assigned roster (or org-wide when p_all is true).
--
-- Why this exists: when a member submits a daily report but forgets to pick an
-- approver group in Swift, the report lands with assigned_approver IS NULL --
-- v_daily_report_approvals exposes this as no_approver_flag
-- (= is_awaiting_approval AND assigned_approver IS NULL). Those reports were only
-- visible in the org-wide "(no approver assigned)" scorecard bucket, effectively
-- a super_admin surface; a lead scoped to their own groups had no way to see the
-- un-assigned reports belonging to THEIR people, so they silently aged with
-- nobody owning them. This backs the ontel-people Home "Missing approver" panel:
-- per lead, exactly the un-assigned reports they should chase (approve directly,
-- or nudge the member to set an approver in Swift).
--
-- Scope reuses analytics.dr_assigned_members_for_email(p_email) verbatim, so the
-- panel's membership is IDENTICAL to the Home "Team members" tile and the roster
-- assignments it derives from (reference.ref_employee_approvers, kind='dr', via
-- migration 182). p_all bypasses the roster filter for the super_admin catch-all,
-- whose own assigned roster is typically empty yet who is expected to see every
-- un-assigned report org-wide.
CREATE OR REPLACE FUNCTION analytics.dr_unassigned_reports_for_email(p_email text, p_all boolean DEFAULT false)
RETURNS TABLE (
  task_did          text,
  emp_id            text,
  employee_name     text,
  member_email      text,
  work_date         date,
  asset_name        text,
  milestone         text,
  submitted_on_et   timestamp,
  pending_wait_days integer
)
LANGUAGE sql
STABLE
SET search_path = analytics
AS $$
  SELECT v.task_did, v.emp_id, v.employee_name, v.email AS member_email,
         v.work_date, v.asset_name, v.milestone, v.submitted_on_et, v.pending_wait_days
  FROM analytics.v_daily_report_approvals v
  WHERE v.no_approver_flag
    AND (
      p_all
      OR v.emp_id IN (SELECT * FROM analytics.dr_assigned_members_for_email(p_email))
    )
  ORDER BY v.pending_wait_days DESC NULLS LAST, v.submitted_on_et ASC;
$$;

REVOKE ALL ON FUNCTION analytics.dr_unassigned_reports_for_email(text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.dr_unassigned_reports_for_email(text, boolean) TO service_role;

-- Semantic-layer metadata for the AI agent.
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'dr_unassigned_reports_for_email' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','dr_unassigned_reports_for_email',
   'Function(p_email text, p_all boolean default false) -> table: the submitted daily reports with NO approver assigned (v_daily_report_approvals.no_approver_flag) that belong to the HR-assigned roster of the approver identified by p_email, ordered oldest-waiting first. Columns: task_did, emp_id, employee_name, member_email, work_date, asset_name, milestone, submitted_on_et, pending_wait_days. Scope reuses analytics.dr_assigned_members_for_email(p_email); p_all=true returns every un-assigned report org-wide (super_admin catch-all). STABLE.',
   'Backs the ontel-people Home "Missing approver" panel. A member who forgets to pick an approver group in Swift leaves the report assigned_approver IS NULL, invisible to their lead''s own-group queues; this scopes those reports to the lead who owns the member (per the roster sheet, migration 182) so the lead can approve them directly or notify the member. p_all covers super_admin, whose own roster is empty but who oversees all un-assigned reports.',
   ARRAY['analytics.v_daily_report_approvals','analytics.dr_assigned_members_for_email','reference.ref_employee_approvers']::text[]);

NOTIFY pgrst, 'reload schema';
