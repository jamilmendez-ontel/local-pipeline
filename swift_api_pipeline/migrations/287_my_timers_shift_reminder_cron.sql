-- =============================================================================
-- 287_my_timers_shift_reminder_cron.sql
-- The clock of the My Timers shift-end reminder email: every 5 minutes pg_cron asks the app's
-- POST /api/reminders/shift-end (net.http_post, pg_net). The app asks the database who is due
-- (app_my_timers.shift_reminders_due), sends the emails and marks the ledger
-- (app_my_timers.shift_reminder). Spec: ontel-my-timers
-- docs/superpowers/specs/2026-10-30-team-demo-rules-design.md 6.4 (D10, D11).
--
-- STATUS: NOT APPLIED. Apply after the app's migrate-production run has put the app migration
--   20261103012715_shift_end_reminder live.
--
-- WHY pg_cron and not Apps Script / GitHub Actions (Jamil, 2026-11-02)
--   * The Apps Script SCHEDULE table fires each job once a day; this needs every 5 minutes.
--   * GitHub cron drifts by up to 20 minutes and costs about 8,600 Action minutes a month here.
--   * The data and the once-per-shift ledger live in OntelDB, so a clock elsewhere adds no uptime.
--
-- THE SECRET (never in git): one random value, in three places.
--   1. Vault, read by the job at each run:
--        select vault.create_secret('<value>', 'my_timers_shift_reminder_key',
--                                   'Bearer of the My Timers shift-end job');
--   2. Vercel production env SHIFT_REMINDER_SECRET = <value> (the route compares it).
--   3. Its sha256 in the app, where shift_reminders_due checks it:
--        insert into app_my_timers.app_setting (key, value)
--        values ('shift_reminder_key_sha256', encode(sha256(convert_to('<value>', 'UTF8')), 'hex'))
--        on conflict (key) do update set value = excluded.value;
--   A missing Vault secret sends no Authorization header: the route answers 401 and nothing else
--   happens.
--
-- THE GATE: Vercel env SHIFT_REMINDER. off (the default) answers {"mode":"off"} and asks the
--   database nothing; preview sends every email to Jamil only; pilot only to SHIFT_REMINDER_PILOT;
--   on to everyone. So the job can run before the INIT-823 invites go out.
--
-- ACTIVATE (after the secret is in all three places):
--   select cron.alter_job((select jobid from cron.job where jobname = 'my-timers-shift-reminder'),
--                         active := true);
-- VERIFY: cron.job_run_details for the job (status succeeded), then net._http_response
--   (status_code 200, content {"mode":"off"} until the gate opens).
--
-- ROLLBACK
--   select cron.unschedule('my-timers-shift-reminder');
--   and delete the Vault secret my_timers_shift_reminder_key in the dashboard if it is not wanted.
-- =============================================================================

select cron.alter_job(cron.schedule(
  'my-timers-shift-reminder',
  '*/5 * * * *',
  $cmd$
  select net.http_post(
    url := 'https://ontel-my-timers.vercel.app/api/reminders/shift-end',
    body := '{}'::jsonb,
    headers := jsonb_strip_nulls(jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || (
        select s.decrypted_secret from vault.decrypted_secrets s
        where s.name = 'my_timers_shift_reminder_key')
    )),
    timeout_milliseconds := 60000
  )
  $cmd$
), active := false);
