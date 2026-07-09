-- 172: second acceptance path for the alias harvest (user proposal 2026-07-09):
-- a submittedBy email that ALREADY BELONGS TO ANOTHER MEMBER in the record can
-- never be attributed to the report's owner. Verified on prod data: every
-- delegate-submitter we caught (Tan Cañete -> Jason, Mary Dela Cruz + Randolf
-- Garcia -> new hires) is themselves in the record, so this rule alone catches
-- all observed failure modes. Residual hole closed with name corroboration:
-- a delegate whose OWN email just changed is not yet in the record, so the
-- submission path additionally requires the personnel object's name to match
-- the report owner's roster name (last name exact + first name or nickname).
--
-- Acceptance paths for the emp<->auth0 link (either suffices):
--   1. TIMER evidence (migration 169): >= 3 attendance-timer rows, >= 80%
--      share. Personal by nature; instant coverage for timer users.
--   2. SUBMISSION evidence (this migration): >= 3 submissions, >= 80% share
--      of the emp's submissions, AND the email is not already recorded for a
--      DIFFERENT emp_id, AND the personnel name matches the emp's roster name.
-- Ambiguity guard applies across the union of both paths.

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
  pairs as (
    select distinct auth0_id, email from tasks
    where auth0_id is not null and email is not null and email <> ''
    union
    select distinct appr_auth0_id, appr_email from tasks
    where appr_auth0_id is not null and appr_email is not null and appr_email <> ''
  ),
  -- Path 1: timer evidence (personal; migration 169 semantics unchanged)
  timer_evidence as (
    select emp_id, user_auth_id as auth0_id, count(*) as n
    from data_staging.stg_daily_report_attendance
    where user_auth_id is not null and user_auth_id <> ''
    group by emp_id, user_auth_id
  ),
  timer_accepted as (
    select e.emp_id, e.auth0_id
    from timer_evidence e
    join (select emp_id, sum(n) as total from timer_evidence group by emp_id) t using (emp_id)
    where e.n >= 3 and e.n::numeric / t.total >= 0.8
  ),
  -- Path 2: submission evidence, gated by not-another-member's-email + name match
  sub_evidence as (
    select emp_id, auth0_id, email, first_name, last_name, count(*) as n
    from tasks
    where auth0_id is not null and email is not null and email <> ''
    group by emp_id, auth0_id, email, first_name, last_name
  ),
  sub_accepted as (
    select s.emp_id, s.auth0_id
    from sub_evidence s
    join (select emp_id, sum(n) as total from sub_evidence group by emp_id) t using (emp_id)
    join reference.ref_employees re on re.emp_id = s.emp_id
    where s.n >= 3
      and s.n::numeric / t.total >= 0.8
      -- the user's rule: an email already recorded for ANOTHER member can
      -- never be read as this member's identity
      and not exists (select 1 from reference.ref_employee_emails ea
                      where ea.email = s.email and ea.emp_id <> s.emp_id)
      -- name corroboration vs the report owner's roster identity
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

update agent.schema_metadata
set description = 'Function() -> integer: full-recompute upsert of Swift-observed aliases into ref_employee_emails. email<->auth0 pairs from submittedBy/approvedBy personnel objects. emp_id<->auth0 accepted via (1) timer evidence (>=3 attendance rows, >=80% share) OR (2) submission evidence (>=3, >=80%) gated by the another-member rule (email already recorded for a different emp_id is never attributed) plus roster name corroboration (last name + first/nickname). Ambiguity guard across both paths. Returns affected rowcount.'
where schema_name = 'reference' and table_name = 'harvest_employee_email_aliases';
