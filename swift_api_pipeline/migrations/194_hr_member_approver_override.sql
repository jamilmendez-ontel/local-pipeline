-- 194: app_hr.hr_member_approver — an app-editable, member-grain OVERLAY that adds
-- daily-report approvers for a member from inside the ontel-people tool, WITHOUT
-- editing HR's authoritative reference.ref_employee_approvers (migration 182,
-- synced daily from the HR sheet). Some people are asked to approve certain
-- members' reports in this tool even though HR's sheet does not (and will not)
-- list them (e.g. Mikaela for all Project Associates; Darren Zammit, who is not
-- even in the roster, for Mikaela and Abbie). This table is that overlay.
--
-- Add-only: reference is never written; "remove" is a hard delete of a row here.
-- The overlay is unioned into analytics.dr_assigned_members_for_email (migration
-- 195, capability/visibility) and analytics.group_approvers (migration 196, the
-- scorecard Approvers list). Live web-app state -> app_hr per the OntelDB schema
-- standard (never reference/data_staging). RLS deny-all; writes go through the
-- app's service_role server actions.

CREATE TABLE app_hr.hr_member_approver (
  member_emp_id   text        NOT NULL,   -- the member being approved (roster emp_id)
  approver_name   text        NOT NULL,   -- display name, e.g. "Darren Zammit"
  approver_email  text        NOT NULL,   -- lowercased; the match key for capability lookups
  approver_emp_id text,                    -- resolved roster emp_id; NULL for non-roster approvers
  note            text,
  created_by      text        NOT NULL,   -- app user email who added the row (audit)
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (member_emp_id, approver_email)
);

-- capability lookups match on lower(approver_email); index it.
CREATE INDEX idx_hr_member_approver_email ON app_hr.hr_member_approver (lower(approver_email));
CREATE INDEX idx_hr_member_approver_member ON app_hr.hr_member_approver (member_emp_id);

ALTER TABLE app_hr.hr_member_approver ENABLE ROW LEVEL SECURITY;
-- deny-all: no policies => only owner / service_role (BYPASSRLS) can touch it.
REVOKE ALL ON app_hr.hr_member_approver FROM anon, authenticated;
GRANT SELECT, INSERT, DELETE ON app_hr.hr_member_approver TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('app_hr','hr_member_approver',
   'App-editable overlay: extra daily-report approvers assigned to a member from inside the ontel-people tool, keyed (member_emp_id, approver_email). approver_emp_id is NULL for non-roster approvers (e.g. management not in the directory). Add-only; removal is a hard delete. created_by/created_at are the audit trail for the in-app review list.',
   'Adds tool-only approver assignments that HR''s authoritative reference.ref_employee_approvers does not carry, without editing that HR-synced source. Unioned into analytics.dr_assigned_members_for_email (who a user may approve / sees as theirs) and analytics.group_approvers (the scorecard Approvers list).',
   ARRAY['reference.ref_employee_approvers','analytics.dr_assigned_members_for_email','analytics.group_approvers']::text[])
ON CONFLICT DO NOTHING;

NOTIFY pgrst, 'reload schema';
