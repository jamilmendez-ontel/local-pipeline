-- =============================================================================
-- 281_app_my_timers_presence_and_nightly.sql
-- The two production steps of the My Timers app that its own migration runner cannot do,
-- applied AFTER the app's migrations (its workflow migrate-production, run #1, 2026-10-12):
--   1. the Realtime presence policies (Online now, super admin piece 2). The runner's role
--      my_timers_owner cannot create a policy in the schema realtime; same exception as
--      DRMC's "app users can read/send presence:app" (ontel-people migration 037).
--      Twin of ontel-my-timers/db/standin/03_presence_policies.sql.
--   2. the nightly rebuild of app_my_timers.task_time_range at 21:30 UTC = 5:30 AM Philippine
--      time, half an hour before the shift day, as pg_cron job my-timers-task-ranges
--      (foundation plan, Task 20 step 5). Built once now so the first members do not wait.
--
-- ROLLBACK:
--   drop policy if exists "app users can read presence:my_timers" on realtime.messages;
--   drop policy if exists "app users can send presence:my_timers" on realtime.messages;
--   select cron.unschedule('my-timers-task-ranges');
--
-- PREFLIGHT (read-only):
--   select policyname from pg_policies where schemaname = 'realtime' and tablename = 'messages';
--     -- DRMC's two "presence:app" policies only; none named presence:my_timers
--   select to_regprocedure('app_my_timers.is_active_app_user()'),
--          to_regprocedure('app_my_timers.refresh_task_time_ranges(timestamptz)');  -- both not null
--   select count(*) from cron.job where jobname = 'my-timers-task-ranges';            -- 0
--
-- STATUS: applied 2026-10-12 via MCP apply_migration. VERIFIED: 2 policies (read SELECT, send INSERT);
-- cron job 39, '30 21 * * *', active; task_time_range 87 rows, min members 5, refreshed 10:51 UTC.
-- =============================================================================
do $$
begin
  if to_regprocedure('app_my_timers.is_active_app_user()') is null then
    raise exception 'app_my_timers.is_active_app_user() does not exist yet. Run the app''s migrate-production first.';
  end if;
  if to_regprocedure('app_my_timers.refresh_task_time_ranges(timestamptz)') is null then
    raise exception 'app_my_timers.refresh_task_time_ranges does not exist yet. Run the app''s migrate-production first.';
  end if;
  if exists (select 1 from pg_policies where schemaname = 'realtime' and policyname like '%presence:my_timers%') then
    raise exception 'presence:my_timers policies already exist. Stop and look.';
  end if;
end $$;

-- 1. Presence policies, scoped to this app's topics only; DRMC's presence:app policies are untouched.
create policy "app users can read presence:my_timers"
  on realtime.messages for select to authenticated
  using ((select realtime.topic()) like 'presence:my\_timers%'
         and realtime.messages.extension = 'presence'
         and app_my_timers.is_active_app_user());
create policy "app users can send presence:my_timers"
  on realtime.messages for insert to authenticated
  with check ((select realtime.topic()) like 'presence:my\_timers%'
              and realtime.messages.extension = 'presence'
              and app_my_timers.is_active_app_user());

-- 2. The nightly job. Build the ranges once now, then every night.
select app_my_timers.refresh_task_time_ranges() as rows_written;
select cron.schedule(
  'my-timers-task-ranges',
  '30 21 * * *',
  $$select app_my_timers.refresh_task_time_ranges()$$
);

-- VERIFY:
--   select policyname, cmd from pg_policies where schemaname = 'realtime' and policyname like '%my_timers%';  -- 2 rows
--   select jobid, jobname, schedule, command, active from cron.job where jobname = 'my-timers-task-ranges';
--   select count(*), min(members), max(refreshed_at) from app_my_timers.task_time_range;  -- min(members) >= 5
--   the morning after:
--   select status, return_message, start_time from cron.job_run_details
--   where jobid = (select jobid from cron.job where jobname = 'my-timers-task-ranges')
--   order by runid desc limit 3;   -- status succeeded (order by runid: a failed run has no start_time)
