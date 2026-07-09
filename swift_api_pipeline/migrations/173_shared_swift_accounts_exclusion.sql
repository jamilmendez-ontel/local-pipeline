-- 173: shared Swift accounts can never be a member's identity. User confirmed
-- "Ontel Management" (auth0:6161e6209bcc1e00687c32c2) belongs to no one; it
-- has run timers on 3 different members' tasks (7 rows total). Below today's
-- thresholds, but a member whose clocks were repeatedly run by a shared
-- account could theoretically reach 3-rows/80% for that ONE member, and the
-- ambiguity guard only trips on 2+ members. Make the exclusion explicit and
-- extensible instead of threshold-dependent.
--
-- The harvest now ignores excluded accounts in BOTH acceptance paths AND in
-- the email pair pool, and also drops their rows from each member's evidence
-- denominator (so a member mostly clocked by a shared account still gets
-- their own identity accepted from their own remaining rows).

create table reference.ref_shared_swift_accounts (
  auth0_id   text primary key,
  label      text not null,
  note       text,
  created_at timestamptz not null default now()
);

alter table reference.ref_shared_swift_accounts enable row level security;
revoke all on reference.ref_shared_swift_accounts from anon, authenticated;
grant select on reference.ref_shared_swift_accounts to service_role;

insert into reference.ref_shared_swift_accounts (auth0_id, label, note) values
  ('auth0:6161e6209bcc1e00687c32c2', 'Ontel Management',
   'Shared admin account; confirmed by Jamil 2026-07-09 as belonging to no individual member. Seen running timers on tasks of 3 different members.');

create or replace function reference.harvest_employee_email_aliases()
returns integer
language plpgsql
security definer
set search_path = reference, data_raw, data_staging
as $$
declare
  affected integer;
begin
  with tasks as (
    select t.emp_id,
           lower(trim(d.d->'submittedBy'->>'email'))   as email,
           d.d->'submittedBy'->'aliasFor'->>'id'       as auth0_id,
           lower(trim(d.d->'submittedBy'->>'firstName')) as first_name,
           lower(trim(d.d->'submittedBy'->>'lastName'))  as last_name,
           lower(trim(d.d->'approvedBy'->>'email'))    as appr_email,
           d.d->'approvedBy'->'aliasFor'->>'id'        as appr_auth0_id
    from (
      select case when jsonb_typeof(data) = 'string' then (data #>> '{}')::jsonb else data end as d,
             task_did
      from data_raw.raw_daily_reports
      where source_type = 'task'
    ) d
    join data_staging.stg_daily_reports t using (task_did)
    where t.emp_id is not null
  ),
  shared as (
    select auth0_id from reference.ref_shared_swift_accounts
  ),
  pairs as (
    select distinct auth0_id, email from tasks
    where auth0_id is not null and email is not null and email <> ''
      and auth0_id not in (select auth0_id from shared)
    union
    select distinct appr_auth0_id, appr_email from tasks
    where appr_auth0_id is not null and appr_email is not null and appr_email <> ''
      and appr_auth0_id not in (select auth0_id from shared)
  ),
  timer_evidence as (
    select emp_id, user_auth_id as auth0_id, count(*) as n
    from data_staging.stg_daily_report_attendance
    where user_auth_id is not null and user_auth_id <> ''
      and user_auth_id not in (select auth0_id from shared)
    group by emp_id, user_auth_id
  ),
  timer_accepted as (
    select e.emp_id, e.auth0_id
    from timer_evidence e
    join (select emp_id, sum(n) as total from timer_evidence group by emp_id) t using (emp_id)
    where e.n >= 3 and e.n::numeric / t.total >= 0.8
  ),
  sub_evidence as (
    select emp_id, auth0_id, email, first_name, last_name, count(*) as n
    from tasks
    where auth0_id is not null and email is not null and email <> ''
      and auth0_id not in (select auth0_id from shared)
    group by emp_id, auth0_id, email, first_name, last_name
  ),
  sub_accepted as (
    select s.emp_id, s.auth0_id
    from sub_evidence s
    join (select emp_id, sum(n) as total from sub_evidence group by emp_id) t using (emp_id)
    join reference.ref_employees re on re.emp_id = s.emp_id
    where s.n >= 3
      and s.n::numeric / t.total >= 0.8
      and not exists (select 1 from reference.ref_employee_emails ea
                      where ea.email = s.email and ea.emp_id <> s.emp_id)
      and s.last_name = lower(trim(re.last_name))
      and (s.first_name = lower(trim(re.first_name))
           or s.first_name = lower(trim(coalesce(re.nickname, ''))))
  ),
  accepted as (
    select emp_id, auth0_id from timer_accepted
    union
    select emp_id, auth0_id from sub_accepted
  ),
  unambiguous as (
    select emp_id, auth0_id from accepted
    where auth0_id in (select auth0_id from accepted group by auth0_id having count(distinct emp_id) = 1)
  )
  insert into reference.ref_employee_emails (emp_id, email, auth0_id, source)
  select u.emp_id, p.email, u.auth0_id, 'swift'
  from unambiguous u
  join pairs p using (auth0_id)
  on conflict (emp_id, email) do update
    set last_seen = now(),
        auth0_id  = coalesce(excluded.auth0_id, ref_employee_emails.auth0_id);
  get diagnostics affected = row_count;
  return affected;
end;
$$;

revoke all on function reference.harvest_employee_email_aliases() from public, anon, authenticated;
grant execute on function reference.harvest_employee_email_aliases() to service_role;

select reference.harvest_employee_email_aliases();

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('reference','ref_shared_swift_accounts',
   'Shared/non-personal Swift accounts (auth0_id, label, note) excluded from all email-alias harvesting: their timer/submission evidence and email pairs are ignored by reference.harvest_employee_email_aliases().',
   'A shared admin account (e.g. Ontel Management) that runs timers or submits on members'' tasks must never be attributed as any member''s identity. Add a row here for any future shared account.',
   ARRAY['reference.ref_employee_emails'])
ON CONFLICT DO NOTHING;
