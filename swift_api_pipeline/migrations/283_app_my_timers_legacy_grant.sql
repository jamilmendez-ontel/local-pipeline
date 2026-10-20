-- =============================================================================
-- 283_app_my_timers_legacy_grant.sql
-- My Timers old history (ontel-my-timers PR #68, feat/legacy-history): my_timers_owner reads
-- the old timer system's six tables so the app can show members their entries from
-- 2025-01-01 and carry the old corrections, removals, runaway drops and additions.
--
-- STATUS: APPLIED 2026-10-20 ~12:35 AM ET via MCP apply_migration (283_app_my_timers_legacy_grant),
--   after ontel-my-timers PR #68 merged (cb648a9) on Jamil's go. Before: RLS on all six, no
--   select, no policy. VERIFIED after: usage on app_timer = true, select on all six = true,
--   my_timers_owner_read policies = 6.
--   Next in the release: migrate-production (word `release`) -> legacy-import dry run ->
--   apply -> second dry run next day -> LEGACY_IMPORT_NIGHTLY=on.
--
-- WHY EACH TABLE
--   app_timer.corrections, entry_removals, entry_additions   the old changes to carry
--   app_timer.duplicate_reviews                               the old runaway rule reads it
--   data_staging.stg_timer_activities                         the old entries (legacy_entry)
--   data_staging.stg_timer_activities_clean                   the go / no-go parity check
-- Read only. my_timers_owner already reads stg_timer_activities_inc, stg_asset_tasks,
-- stg_assets, ref_employee_emails and ref_swift_users (migration 280).
--
-- RLS is ON for all six (checked on production 2026-10-19) and my_timers_owner has no
-- BYPASSRLS: a table with RLS and no policy for the role reads as EMPTY, not as an error.
-- So every table needs its policy. (The app's refresh also refuses to run when the old rows
-- read as fewer than half of the live ones, so a lost policy cannot empty the history.)
--
-- DEPENDENCY CREATED BY THE APP (not by this file): app_my_timers.v_timer_entry depends on
-- data_staging.stg_timer_activities_inc. A DROP ... CASCADE of that table drops the view and
-- breaks every member page. Drop and re-create the view through a My Timers migration.
--
-- ROLLBACK
--   drop policy if exists my_timers_owner_read on data_staging.stg_timer_activities_clean;
--   drop policy if exists my_timers_owner_read on data_staging.stg_timer_activities;
--   drop policy if exists my_timers_owner_read on app_timer.duplicate_reviews;
--   drop policy if exists my_timers_owner_read on app_timer.entry_additions;
--   drop policy if exists my_timers_owner_read on app_timer.entry_removals;
--   drop policy if exists my_timers_owner_read on app_timer.corrections;
--   revoke select on data_staging.stg_timer_activities, data_staging.stg_timer_activities_clean
--     from my_timers_owner;
--   revoke select on app_timer.corrections, app_timer.entry_removals, app_timer.entry_additions,
--     app_timer.duplicate_reviews from my_timers_owner;
--   revoke usage on schema app_timer from my_timers_owner;
--
-- VERIFY (after apply): each returns true / a count above 0 when run as my_timers_owner:
--   select has_schema_privilege('my_timers_owner', 'app_timer', 'usage');
--   select count(*) from pg_policies where policyname = 'my_timers_owner_read'
--     and (schemaname, tablename) in (('app_timer','corrections'), ('app_timer','entry_removals'),
--       ('app_timer','entry_additions'), ('app_timer','duplicate_reviews'),
--       ('data_staging','stg_timer_activities'), ('data_staging','stg_timer_activities_clean'));
--   -- expect 6
-- =============================================================================

grant usage on schema app_timer to my_timers_owner;

grant select on app_timer.corrections, app_timer.entry_removals, app_timer.entry_additions,
  app_timer.duplicate_reviews to my_timers_owner;
grant select on data_staging.stg_timer_activities, data_staging.stg_timer_activities_clean
  to my_timers_owner;

create policy my_timers_owner_read on app_timer.corrections
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on app_timer.entry_removals
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on app_timer.entry_additions
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on app_timer.duplicate_reviews
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on data_staging.stg_timer_activities
  for select to my_timers_owner using (true);
create policy my_timers_owner_read on data_staging.stg_timer_activities_clean
  for select to my_timers_owner using (true);
