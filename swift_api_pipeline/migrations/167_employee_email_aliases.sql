-- 167: person-resolution email aliases. One row per (emp_id, email) a person
-- has EVER had, so email changes (marriage, surname migration) never orphan
-- their timer history, approver attribution, or group routing.
--
-- DUAL-SOURCE, first-one-wins (user decision 2026-07-09):
--   'roster' — trigger below fires when the HR sheet sync inserts/updates an
--     email on reference.ref_employees. CRITICAL because sync_employees.py
--     updates email IN PLACE (one row per emp_id), destroying the old value;
--     the alias table is the only place the old address survives.
--   'swift' — harvest fn below reads submittedBy.email + submittedBy.aliasFor.id
--     (the permanent auth0 account id) from raw daily-report task payloads,
--     joined to stg_daily_reports for emp_id. Called by the daily-reports
--     pipeline each run, so a member submitting under a new Swift email
--     registers within one pipeline cycle even before HR updates the sheet.
-- Rows are never deleted; first_seen/last_seen bound each address's life.

create table reference.ref_employee_emails (
  emp_id     text not null,
  email      text not null,
  auth0_id   text,
  source     text not null check (source in ('roster_seed','roster','swift','manual')),
  first_seen timestamptz not null default now(),
  last_seen  timestamptz not null default now(),
  note       text,
  primary key (emp_id, email)
);

create index idx_ref_employee_emails_email on reference.ref_employee_emails (email);

alter table reference.ref_employee_emails enable row level security;
revoke all on reference.ref_employee_emails from anon, authenticated;
grant select, insert, update on reference.ref_employee_emails to service_role;

-- Roster-side feed: any insert or email update on ref_employees upserts the
-- (possibly new) address. The previous address's row already exists from the
-- seed or an earlier firing, so history is preserved by never deleting.
create or replace function reference.trg_employee_email_alias()
returns trigger
language plpgsql
security definer
set search_path = reference
as $$
begin
  if new.email is not null and length(trim(new.email)) > 0 then
    insert into reference.ref_employee_emails (emp_id, email, source)
    values (new.emp_id, lower(trim(new.email)), 'roster')
    on conflict (emp_id, email) do update set last_seen = now();
  end if;
  return new;
end;
$$;

create trigger ref_employees_email_alias
after insert or update of email on reference.ref_employees
for each row execute function reference.trg_employee_email_alias();

-- Swift-side feed: harvest (emp_id, email, auth0_id) triples from raw daily
-- report task payloads. data is stored as a jsonb STRING for these rows, so
-- unwrap before extracting. p_run_id null = full-history backfill.
create or replace function reference.harvest_employee_email_aliases(p_run_id uuid default null)
returns integer
language plpgsql
security definer
set search_path = reference, data_raw, data_staging
as $$
declare
  n integer;
begin
  insert into reference.ref_employee_emails (emp_id, email, auth0_id, source)
  select distinct
    t.emp_id,
    lower(trim(d.d->'submittedBy'->>'email')),
    d.d->'submittedBy'->'aliasFor'->>'id',
    'swift'
  from (
    select case when jsonb_typeof(data) = 'string' then (data #>> '{}')::jsonb else data end as d,
           task_did
    from data_raw.raw_daily_reports
    where source_type = 'task'
      and (p_run_id is null or run_id = p_run_id)
  ) d
  join data_staging.stg_daily_reports t using (task_did)
  where d.d->'submittedBy'->>'email' is not null
    and length(trim(d.d->'submittedBy'->>'email')) > 0
    and t.emp_id is not null
  on conflict (emp_id, email) do update
    set last_seen = now(),
        auth0_id  = coalesce(excluded.auth0_id, ref_employee_emails.auth0_id);
  get diagnostics n = row_count;
  return n;
end;
$$;

revoke all on function reference.trg_employee_email_alias() from public, anon, authenticated;
revoke all on function reference.harvest_employee_email_aliases(uuid) from public, anon, authenticated;
grant execute on function reference.harvest_employee_email_aliases(uuid) to service_role;

-- Seed from the current roster, then backfill from all raw report history.
insert into reference.ref_employee_emails (emp_id, email, source)
select emp_id, lower(trim(email)), 'roster_seed'
from reference.ref_employees
where email is not null and length(trim(email)) > 0
on conflict (emp_id, email) do nothing;

select reference.harvest_employee_email_aliases(null);

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('reference','ref_employee_emails',
   'Person-resolution email alias map: one row per (emp_id, email) an employee has ever had, with optional Swift auth0_id, source (roster_seed/roster/swift/manual), first_seen/last_seen. Fed by a trigger on ref_employees (HR roster updates) AND reference.harvest_employee_email_aliases() (submittedBy email+auth0 id from raw daily-report payloads, run each pipeline cycle). Rows are never deleted.',
   'Makes emp_id the person anchor across the warehouse: timer rollups, report review, and approver attribution resolve emails through this table, so a member changing email (marriage, surname migration) keeps one unbroken history. Whichever source sees the new address first (HR sheet or a Swift submission) wins.',
   ARRAY['reference.ref_employees','data_raw.raw_daily_reports','analytics.mv_timer_day_rollup']),
  ('reference','harvest_employee_email_aliases',
   'Function(p_run_id uuid default null) -> integer: upserts (emp_id, lower(submittedBy.email), submittedBy.aliasFor.id auth0) triples into ref_employee_emails from data_raw.raw_daily_reports task payloads (joined to stg_daily_reports for emp_id). Null run id = full-history backfill. Returns affected rowcount.',
   'Swift-side feed of the alias map, called by extract_daily_reports.py each run so email changes register within one pipeline cycle.',
   ARRAY['reference.ref_employee_emails','data_raw.raw_daily_reports','data_staging.stg_daily_reports'])
ON CONFLICT DO NOTHING;
