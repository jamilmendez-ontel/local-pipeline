-- 204: analytics.dr_assigned_groups_for_email(p_email) -- the distinct DR queue
-- labels a given app user is ASSIGNED to approve, from HR's roster sheet
-- (reference.ref_employee_approvers.approver_group, migration 182).
--
-- This is the group-level twin of analytics.dr_assigned_members_for_email
-- (migration 182/195) and the canonical replacement for the approval-HISTORY
-- inference in analytics.approver_groups_for_email (migration 159), which the
-- ontel-people Home page uses to scope the Pending-approvals KPI, its browse
-- deep-link, and the "Your groups" breakdown.
--
-- Why the swap: approver_groups_for_email credits a user with EVERY queue they
-- have ever cleared a report in. An approver who once covered another team's
-- queue keeps it forever, so that queue's reports leak into their Home numbers.
-- Observed on prod 2026-07-30: akira@ontel.co's history-derived groups include
-- "Kris Kerr" (60 submitted reports he does not own) alongside his 3 real
-- roster queues; the reference table lists only the 3 he is actually assigned.
--
-- Identity resolution mirrors dr_assigned_members_for_email exactly: alias-first
-- via reference.ref_employee_emails matched on the resolved approver_emp_id,
-- with a direct approver_email match as fallback so non-roster approvers (whose
-- assignments carry an override email but no emp_id) still resolve.
--
-- Deliberately roster-ONLY (unlike the members RPC, which unions the
-- app_hr.hr_member_approver overlay in migration 195): the overlay is
-- member-grain, so unioning an overlaid member's queue label would re-grant the
-- user that member's WHOLE queue -- the same over-inclusion this migration
-- exists to remove. The app keeps approver_groups_for_email as a fallback for
-- approvers the roster sheet does not list, so overlay-only approvers are
-- unaffected.
create or replace function analytics.dr_assigned_groups_for_email(p_email text)
returns setof text
language sql
stable
set search_path = analytics
as $$
  with me as (
    select (select ea.emp_id
            from reference.ref_employee_emails ea
            where ea.email = lower(p_email)
            order by ea.last_seen desc
            limit 1) as emp_id
  )
  select distinct a.approver_group
  from me, reference.ref_employee_approvers a
  where a.kind = 'dr'
    and a.approver_group is not null
    and length(trim(a.approver_group)) > 0
    and (
      (a.approver_emp_id is not null and a.approver_emp_id = me.emp_id)
      or lower(a.approver_email) = lower(p_email)
    );
$$;

revoke all on function analytics.dr_assigned_groups_for_email(text) from public;
grant execute on function analytics.dr_assigned_groups_for_email(text) to service_role;

-- Semantic-layer metadata for the AI agent.
delete from agent.schema_metadata
 where schema_name = 'analytics' and table_name = 'dr_assigned_groups_for_email' and column_name is null;
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
values
  ('analytics', 'dr_assigned_groups_for_email',
   'Function(p_email text) -> setof text: the distinct DR queue labels (approver_group) a given app user is ASSIGNED to approve, from reference.ref_employee_approvers (HR sheet, kind=dr). Alias-first identity via reference.ref_employee_emails matched on approver_emp_id, direct email match as fallback for non-roster approvers. Roster-only (does NOT union the app_hr.hr_member_approver overlay, which is member-grain). STABLE.',
   'Canonical replacement for approver_groups_for_email (which infers queues from approval history and drifts, leaking queues a user once covered): backs the ontel-people Home Pending-approvals KPI scope, its browse deep-link, and the "Your groups" breakdown. The app falls back to the history RPC only when this returns no rows (approvers the roster sheet does not list).',
   array['reference.ref_employee_approvers', 'reference.ref_employee_emails']::text[]);

notify pgrst, 'reload schema';
