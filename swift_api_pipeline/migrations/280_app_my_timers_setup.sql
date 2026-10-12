-- =============================================================================
-- 280_app_my_timers_setup.sql
-- One-time setup of the schema app_my_timers for the My Timers app
-- (repo jamilmendez-ontel/ontel-my-timers). After this file, every object of the schema
-- is created by the app's own migration runner (scripts/migrate.ts) through the app's
-- migrate-production workflow, which logs in as my_timers_owner and nothing else.
-- This file is the production twin of ontel-my-timers/db/standin/02_setup.sql.
--
-- WHAT IS DIFFERENT FROM THE OTHER app_* SCHEMAS, AND WHY
--   DATABASE_ARCHITECTURE.md says: revoke USAGE from anon and authenticated, and no
--   SECURITY DEFINER function unless reviewed. This schema grants USAGE to `authenticated`
--   and its member-facing functions are SECURITY DEFINER. That is the design approved on
--   2026-09-28 (release go 2026-10-12): the app holds no service key, a member calls the
--   functions with the member's own session, and each function returns only the rows of
--   the employee behind the email in that session. `anon` has no right at all. Each
--   function: no email or user id parameter, search_path = '', EXECUTE revoked from PUBLIC
--   and anon, tested that member A cannot receive a row of member B (409 database tests).
--
-- The password is NOT in this file. After applying, set it by hand:
--   alter role my_timers_owner password '<a new long password>';
-- and keep it only in the GitHub secret PRODUCTION_DATABASE_URL of the app's repository.
--
-- ROLLBACK (in this order; safe while no member uses the app):
--   -- 1. take the schema off the API list: run the block of section 5 with the name removed
--   drop policy if exists my_timers_owner_read on data_staging.stg_timer_activities_inc;
--   drop policy if exists my_timers_owner_read on reference.ref_swift_users;
--   drop policy if exists my_timers_owner_read on reference.ref_employee_emails;
--   drop policy if exists my_timers_owner_read on data_staging.stg_assets;
--   drop policy if exists my_timers_owner_read on data_staging.stg_asset_tasks;
--   drop schema app_my_timers cascade;
--   revoke all on data_staging.stg_timer_activities_inc, analytics.v_employee_directory,
--     reference.ref_swift_users, reference.ref_employee_emails, analytics.mv_hr_report_review,
--     data_staging.stg_assets, data_staging.stg_asset_tasks from my_timers_owner;
--   revoke usage on schema data_staging, analytics, reference from my_timers_owner;
--   drop role my_timers_owner;
--   delete from agent.schema_metadata where schema_name = 'app_my_timers';
--
-- PREFLIGHT 2026-10-12 (read-only, before apply): role 0, schema 0, metadata 0; the seven
-- sources exist and match the stand-ins column for column (35/24/6/7/38/18/30); RLS is on
-- for the five tables that get a policy (the materialized view has none); pgrst.db_schemas =
-- public, data_raw, data_staging, pipeline, agent, analytics, reference, app_quote, app_hr,
-- app_reports; the duplicate-email query of the app's README returns 0 rows.
--
-- STATUS: applied 2026-10-12 via MCP apply_migration; verification results in the PR body.
-- =============================================================================
BEGIN;

-- 1. The owner role. The migrate-production workflow logs in as this role and nothing else.
create role my_timers_owner login noinherit connection limit 5;
grant my_timers_owner to postgres;

-- 2. The schema.
create schema app_my_timers authorization my_timers_owner;
revoke all on schema app_my_timers from public, anon;
grant usage on schema app_my_timers to authenticated;

-- 3. What the owner may read: the seven sources, nothing else.
grant usage on schema data_staging, analytics, reference to my_timers_owner;
grant select on data_staging.stg_timer_activities_inc to my_timers_owner;
grant select on analytics.v_employee_directory to my_timers_owner;
grant select on reference.ref_swift_users to my_timers_owner;
grant select on reference.ref_employee_emails to my_timers_owner;
grant select on analytics.mv_hr_report_review to my_timers_owner;
grant select on data_staging.stg_assets to my_timers_owner;
grant select on data_staging.stg_asset_tasks to my_timers_owner;
create policy my_timers_owner_read on data_staging.stg_timer_activities_inc
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on reference.ref_swift_users
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on reference.ref_employee_emails
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on data_staging.stg_assets
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on data_staging.stg_asset_tasks
  for select to my_timers_owner using (true);

-- 4. The table the migration runner keeps its list in.
create table app_my_timers.schema_migrations (
  version    text primary key,
  name       text not null,
  checksum   text not null,
  applied_at timestamptz not null default now()
);
alter table app_my_timers.schema_migrations owner to my_timers_owner;
alter table app_my_timers.schema_migrations enable row level security;
revoke all on app_my_timers.schema_migrations from public, anon, authenticated;

-- 5. The API serves only the schemas on its list. The list is read first and the name is
--    added to it, so a schema another app added in the meantime is kept.
do $$
declare
  v_setting text;
  v_list text;
begin
  select s into v_setting
  from pg_roles r, unnest(r.rolconfig) as s
  where r.rolname = 'authenticator' and s like 'pgrst.db_schemas=%';
  if v_setting is null then
    raise exception 'pgrst.db_schemas is not set on the role authenticator. Stop and look.';
  end if;
  v_list := substr(v_setting, length('pgrst.db_schemas=') + 1);
  if 'app_my_timers' = any (select btrim(x) from unnest(string_to_array(v_list, ',')) as x) then
    raise notice 'app_my_timers is on the list already: %', v_list;
  else
    execute format('alter role authenticator set pgrst.db_schemas = %L', v_list || ', app_my_timers');
    raise notice 'list is now: %', v_list || ', app_my_timers';
  end if;
end $$;

-- 6. The record for the AI semantic layer.
insert into agent.schema_metadata
  (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
select v.schema_name, v.table_name, null, v.description, v.business_context, v.data_notes, v.related_tables
from (values
  ('app_my_timers', 'schema_migrations',
   'List of the migration files of the My Timers app that were applied, with their checksums.',
   'Written only by the app''s migration runner (scripts/migrate.ts) in its migrate-production workflow.',
   'A file whose checksum differs from the one stored here was edited after it was applied; the runner refuses it.',
   array[]::text[]),
  ('app_my_timers', 'task_time_range',
   'Usual range (25th to 75th percentile, minutes) of the time a task takes, per carrier and task, over the last 30 days.',
   'Read by the member-facing functions of My Timers to show "my time per task" beside the team. Holds no names and no emails.',
   'Rebuilt every night by the pg_cron job my-timers-task-ranges (5:30 AM Philippine time). A row exists only for 5 or more members. carrier = __everyone__ is the range over every carrier.',
   array['data_staging.stg_timer_activities_inc', 'analytics.v_employee_directory'])
) as v(schema_name, table_name, description, business_context, data_notes, related_tables)
where not exists (
  select 1 from agent.schema_metadata m
  where m.schema_name = v.schema_name and m.table_name = v.table_name and m.column_name is null
);

COMMIT;
notify pgrst, 'reload config';

-- VERIFY (after the commit):
--   select rolname, rolcanlogin, rolbypassrls, rolsuper, rolconnlimit from pg_roles where rolname = 'my_timers_owner';
--     -- login true, bypass false, super false, limit 5
--   select nspname, pg_get_userbyid(nspowner) from pg_namespace where nspname = 'app_my_timers';
--     -- owner my_timers_owner
--   select has_schema_privilege('anon', 'app_my_timers', 'usage'),            -- false
--          has_schema_privilege('authenticated', 'app_my_timers', 'usage'),   -- true
--          has_table_privilege('authenticated', 'app_my_timers.schema_migrations', 'select'); -- false
--   select unnest(rolconfig) from pg_roles where rolname = 'authenticator';   -- list ends with app_my_timers
--   select count(*) from agent.schema_metadata where schema_name = 'app_my_timers';  -- 2
--   select count(*) from pg_policy where polname = 'my_timers_owner_read';          -- 5
--   set role my_timers_owner;
--   select count(*) from data_staging.stg_timer_activities_inc;   -- more than 0
--   select count(*) from data_staging.stg_timer_activities;       -- ERROR: permission denied
--   reset role;
-- Then confirm in the dashboard (Settings, API, Exposed schemas) that app_my_timers is listed,
-- so a later save there does not drop it.
