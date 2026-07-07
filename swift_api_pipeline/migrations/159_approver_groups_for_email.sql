-- 159: analytics.approver_groups_for_email(p_email) -- which approver queues a
-- given app user actually works, for the ontel-people Home page KPI scoping.
--
-- The scorecard/approval queue are group-grain, keyed by the assigned_approver
-- label on each report. That label may be a group ("Daily Report Approvers - CG1")
-- OR an individual lead's name ("Darren Zammit"). It does NOT tell you who
-- approves: a queue named after a person is often cleared by someone else
-- (e.g. reports assigned to "Darren Zammit" are approved by Merjien). So ownership
-- can only come from approval HISTORY (approved_by), never from the label.
--
-- Bridge: the Home page has the signed-in user's email. v_employee_directory maps
-- email -> name parts; v_daily_report_approvals.approved_by is the Swift display
-- name. The directory stores full legal names ("Jonathan Garcia Canete") while
-- Swift uses short display names ("Tan Canete"), so exact full-name match returns
-- nothing -- match on nickname/first + last name. Verified on prod 2026-07-07:
-- tan.canete@ -> CG3 (949), orville@ -> 3 groups (1212), hajie@ -> DA&R (270),
-- jamil.mendez@ -> 0 (does not approve daily reports).
CREATE OR REPLACE FUNCTION analytics.approver_groups_for_email(p_email text)
RETURNS SETOF text
LANGUAGE sql
STABLE
SET search_path = analytics, public
AS $$
  SELECT DISTINCT a.assigned_approver
  FROM analytics.v_employee_directory d
  JOIN analytics.v_daily_report_approvals a
    ON a.approved_by IS NOT NULL
   AND (
        lower(a.approved_by) = lower(coalesce(d.nickname, '')   || ' ' || coalesce(d.last_name, ''))
     OR lower(a.approved_by) = lower(coalesce(d.first_name, '') || ' ' || coalesce(d.last_name, ''))
   )
  WHERE lower(d.email) = lower(p_email)
    AND a.assigned_approver IS NOT NULL;
$$;

REVOKE ALL ON FUNCTION analytics.approver_groups_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_groups_for_email(text) TO service_role;

-- Semantic-layer metadata (object-level row: column_name NULL).
DELETE FROM agent.schema_metadata
 WHERE schema_name = 'analytics' AND table_name = 'approver_groups_for_email' AND column_name IS NULL;
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','approver_groups_for_email',
   'Function(p_email text) -> setof text: the distinct assigned_approver queue labels a given app user has actually approved, derived from approval history (v_daily_report_approvals.approved_by matched to v_employee_directory by nickname/first + last name). STABLE.',
   'Backs the ontel-people Home page: scopes the Pending-approvals and On-time-rate KPIs to the queues a user personally works. Ownership comes from who approved, not from the queue label (a queue named after a lead is often cleared by someone else).',
   ARRAY['analytics.v_daily_report_approvals','analytics.v_employee_directory']::text[]);

-- Make the new function visible to PostgREST immediately (for supabase-js .rpc).
NOTIFY pgrst, 'reload schema';
