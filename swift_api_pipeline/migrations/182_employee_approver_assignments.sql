-- 182: canonical DR/leave approver assignments from the HR roster sheet.
--
-- HR added four columns to Ms. Orv's "Active Employee Information" sheet
-- (the same sheet sync_employees.py already reads):
--   'Approvers'                (the member's DR queue label, e.g. "Daily Report Approvers - CG1")
--   'DR Approvers Individuals' (comma-separated people who approve this member's daily reports)
--   'Leave Approvers'          (comma-separated people who approve this member's leaves)
--   'Leave CC'                 (comma-separated people CC'd on leave requests)
-- sync_employees.py lands them here on every roster sync (upsert + prune, so
-- the table is never momentarily empty), resolving each approver name to a
-- roster emp_id/email where possible. Non-roster approvers (management, e.g.
-- Justin Bailey / Kris Kerr) resolve through an override map in the script;
-- unresolvable names land with NULL emp_id/email and are logged.
--
-- This replaces approval HISTORY as the source of "who approves whom":
-- analytics.approved_members_for_email (migrations 161/171) infers the team
-- from approvals in the trailing 180 days, which drifts as coverage rotates.
-- The sheet is HR's authoritative assignment; history remains only a fallback
-- for approvers the sheet does not list.

create table reference.ref_employee_approvers (
  emp_id          text not null,      -- the member being approved (roster emp_id)
  kind            text not null check (kind in ('dr', 'leave', 'leave_cc')),
  approver_name   text not null,      -- exactly as written in the sheet cell
  approver_emp_id text,               -- resolved roster emp_id (null: non-roster/unresolved)
  approver_email  text,               -- resolved email, lowercased (null: unresolved)
  approver_group  text,               -- the member's 'Approvers' queue label from the sheet
  rank            smallint not null default 1,  -- position within the sheet cell
  synced_at       timestamptz not null default now(),
  primary key (emp_id, kind, approver_name)
);

create index idx_ref_employee_approvers_email on reference.ref_employee_approvers (approver_email);
create index idx_ref_employee_approvers_emp on reference.ref_employee_approvers (approver_emp_id);

alter table reference.ref_employee_approvers enable row level security;
revoke all on reference.ref_employee_approvers from anon, authenticated;
grant select, insert, update, delete on reference.ref_employee_approvers to service_role;

-- The members a given app user is ASSIGNED to approve daily reports for.
-- Mirrors the shape of analytics.approved_members_for_email (setof emp_id text)
-- so the ontel-people Home tile and /directory?approver= drill-down can switch
-- over without reshaping. Identity: alias-first through ref_employee_emails
-- (like migration 171) matched on the resolved approver_emp_id, with a direct
-- email match as fallback so non-roster approvers (whose assignments carry an
-- override email but no emp_id) still resolve. Joins the directory so only
-- emp_ids the /directory list can render are returned.
create or replace function analytics.dr_assigned_members_for_email(p_email text)
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
  select distinct a.emp_id
  from me, reference.ref_employee_approvers a
  join analytics.v_employee_directory d on d.emp_id = a.emp_id
  where a.kind = 'dr'
    and (
      (a.approver_emp_id is not null and a.approver_emp_id = me.emp_id)
      or lower(a.approver_email) = lower(p_email)
    );
$$;

revoke all on function analytics.dr_assigned_members_for_email(text) from public;
grant execute on function analytics.dr_assigned_members_for_email(text) to service_role;

-- Semantic-layer metadata for the AI agent.
delete from agent.schema_metadata
 where schema_name = 'reference' and table_name = 'ref_employee_approvers' and column_name is null;
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
values
  ('reference', 'ref_employee_approvers',
   'One row per (member emp_id, kind, approver name): who is assigned to approve each employee''s daily reports (kind=dr), leaves (kind=leave), or be CC''d on leaves (kind=leave_cc). Synced from the HR "Active Employee Information" Google Sheet by sync_employees.py (upsert + prune each run). approver_emp_id/approver_email are resolved from the roster where possible; approver_group carries the member''s DR queue label (e.g. "Daily Report Approvers - CG1"). rank preserves the order within the sheet cell.',
   'HR''s authoritative "who approves whom" assignment, replacing approval-history inference. Backs analytics.dr_assigned_members_for_email and thus the ontel-people Home "Members you approve" tile and /directory?approver= drill-down.',
   array['reference.ref_employees', 'reference.ref_employee_emails']::text[]);

delete from agent.schema_metadata
 where schema_name = 'analytics' and table_name = 'dr_assigned_members_for_email' and column_name is null;
insert into agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
values
  ('analytics', 'dr_assigned_members_for_email',
   'Function(p_email text) -> setof text: the distinct emp_ids of employees a given app user is ASSIGNED to approve daily reports for, from reference.ref_employee_approvers (HR sheet). Alias-first identity via reference.ref_employee_emails matched on approver_emp_id, direct email match as fallback for non-roster approvers. Joined to v_employee_directory so only directory-resolvable emp_ids return. STABLE.',
   'Canonical replacement for approved_members_for_email (which infers the team from 180-day approval history and drifts): backs the ontel-people Home "Members you approve" tile and its /directory?approver= drill-down. The app falls back to the history RPC only when this returns no rows.',
   array['reference.ref_employee_approvers', 'reference.ref_employee_emails', 'analytics.v_employee_directory']::text[]);

notify pgrst, 'reload schema';
