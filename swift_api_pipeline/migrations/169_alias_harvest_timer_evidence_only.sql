-- 169: tighten 168's emp<->auth0 acceptance rule. 168 counted task
-- SUBMISSIONS as identity evidence, but submissions are delegable: emp 251104
-- (Glenie Laggui, new hire, zero self-activity) had 10/11 of her reports
-- submitted by coordinator Mary Dela Cruz, so 168's majority rule wrongly
-- aliased mary@ontel.co to Glenie. Timers are personal: the attendance
-- user_auth_id is whoever physically ran the clock. So the emp<->auth0 link
-- now accepts TIMER EVIDENCE ONLY (>= 3 timer rows and >= 80% of that emp's
-- timer identity evidence), plus an ambiguity guard (an auth0 accepted for
-- two emp_ids is dropped for both). Verified outcomes on prod data:
--   emp 220502 Jehane Ong Abilay: 103 timers under one auth0 -> ACCEPTED;
--     links old jehane.ong@ + current jehane.abilay@ (real marriage rename).
--   emp 251104 Glenie Laggui: zero timers -> no Swift alias (roster trigger
--     still covers her; Swift side activates once she runs timers).
-- email<->auth0 pairs (from submittedBy/approvedBy personnel objects) are
-- unchanged: those bind an email to ITS OWN auth0 and are always consistent.

delete from reference.ref_employee_emails where source = 'swift';

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
    select lower(trim(d.d->'submittedBy'->>'email'))   as email,
           d.d->'submittedBy'->'aliasFor'->>'id'       as auth0_id,
           lower(trim(d.d->'approvedBy'->>'email'))    as appr_email,
           d.d->'approvedBy'->'aliasFor'->>'id'        as appr_auth0_id
    from (
      select case when jsonb_typeof(data) = 'string' then (data #>> '{}')::jsonb else data end as d
      from data_raw.raw_daily_reports
      where source_type = 'task'
    ) d
  ),
  -- (a) email <-> auth0 pairs, from submitter AND approver personnel objects
  pairs as (
    select distinct auth0_id, email from tasks
    where auth0_id is not null and email is not null and email <> ''
    union
    select distinct appr_auth0_id, appr_email from tasks
    where appr_auth0_id is not null and appr_email is not null and appr_email <> ''
  ),
  -- (b) emp <-> auth0: TIMER evidence only (timers are personal, submissions
  -- are delegable)
  evidence as (
    select emp_id, user_auth_id as auth0_id, count(*) as n
    from data_staging.stg_daily_report_attendance
    where user_auth_id is not null and user_auth_id <> ''
    group by emp_id, user_auth_id
  ),
  accepted as (
    select e.emp_id, e.auth0_id
    from evidence e
    join (select emp_id, sum(n) as total from evidence group by emp_id) t using (emp_id)
    where e.n >= 3 and e.n::numeric / t.total >= 0.8
  ),
  -- Ambiguity guard: an auth0 that majority-matches two different emp_ids is
  -- untrustworthy for both.
  unambiguous as (
    select emp_id, auth0_id from accepted
    where auth0_id in (select auth0_id from accepted group by auth0_id having count(*) = 1)
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
set description = 'Function() -> integer: full-recompute upsert of Swift-observed aliases into ref_employee_emails. email<->auth0 pairs come from submittedBy/approvedBy personnel objects in raw daily-report payloads. emp_id<->auth0 links accept TIMER evidence only (>=3 attendance-timer rows and >=80% share for that emp, timers being personal while submissions are delegable), with an ambiguity guard dropping any auth0 that majority-matches two emps. Returns affected rowcount.'
where schema_name = 'reference' and table_name = 'harvest_employee_email_aliases';
