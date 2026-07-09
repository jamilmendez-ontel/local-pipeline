-- 168: fix 167's Swift alias harvest. 167 assumed a task's submittedBy is the
-- task's owner; in reality leads submit reports ON BEHALF of members
-- (verified: emp 220316 Jason Nobleza = 133 self-submissions + 1 by Tan
-- Cañete), so 34 wrong (emp_id, email) alias rows landed. Nothing consumed
-- them yet (rollup rekey not applied). Corrected design splits the claim into
-- two independently trustworthy links:
--   (a) email <-> auth0: from inside one personnel object (submittedBy /
--       approvedBy bind an email to its permanent aliasFor auth0 id). Always
--       internally consistent, regardless of whose task carries the object.
--   (b) emp_id <-> auth0: MAJORITY EVIDENCE ONLY. Combine per-emp counts of
--       task submitter identities and attendance-timer user identities;
--       accept an auth0 for an emp only with >= 3 observations AND >= 80%
--       share of that emp's total evidence. A one-off delegated submission
--       can never pass; the owner's own identity always does.
-- Alias rows = accepted (emp_id, auth0) joined to all (auth0, email) pairs,
-- which also captures a person's OLD addresses still present in raw history.
-- Full recompute each call (idempotent upsert; ~25k raw task rows, cheap), so
-- the per-run uuid filter is gone: drop the old signature.

delete from reference.ref_employee_emails where source = 'swift';

drop function if exists reference.harvest_employee_email_aliases(uuid);

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
  -- (a) email <-> auth0 pairs, from submitter AND approver objects
  pairs as (
    select distinct auth0_id, email from tasks
    where auth0_id is not null and email is not null and email <> ''
    union
    select distinct appr_auth0_id, appr_email from tasks
    where appr_auth0_id is not null and appr_email is not null and appr_email <> ''
  ),
  -- (b) emp <-> auth0 evidence: task submitter identity + timer user identity
  evidence as (
    select emp_id, auth0_id, count(*) as n
    from (
      select emp_id, auth0_id from tasks where auth0_id is not null
      union all
      select emp_id, user_auth_id as auth0_id
      from data_staging.stg_daily_report_attendance
      where user_auth_id is not null and user_auth_id <> ''
    ) e
    group by emp_id, auth0_id
  ),
  accepted as (
    select e.emp_id, e.auth0_id
    from evidence e
    join (select emp_id, sum(n) as total from evidence group by emp_id) t using (emp_id)
    where e.n >= 3 and e.n::numeric / t.total >= 0.8
  )
  insert into reference.ref_employee_emails (emp_id, email, auth0_id, source)
  select a.emp_id, p.email, a.auth0_id, 'swift'
  from accepted a
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
set description = 'Function() -> integer: full-recompute upsert of Swift-observed aliases into ref_employee_emails. email<->auth0 pairs come from submittedBy/approvedBy personnel objects in raw daily-report payloads; emp_id<->auth0 links are accepted only on majority evidence (>=3 observations and >=80% share across task submissions + attendance-timer identities), so delegated submissions cannot create false aliases. Returns affected rowcount.'
where schema_name = 'reference' and table_name = 'harvest_employee_email_aliases';
