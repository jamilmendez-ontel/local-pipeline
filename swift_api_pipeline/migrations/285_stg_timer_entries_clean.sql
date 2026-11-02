-- =============================================================================
-- 285_stg_timer_entries_clean.sql
-- data_staging.stg_timer_entries_clean: one row per timer entry as the My Timers app lays it
-- out for every member (all fixes laid on, removed entries out, approved missing entries in),
-- with counted_minutes capped at 10 hours, refreshed by pg_cron, and checked every night
-- against v1's data_staging.stg_timer_activities_clean per member-day.
-- Plan: ontel-my-timers docs/superpowers/plans/2026-11-02-entries-clean-table.md (Task 2);
-- spec: ontel-my-timers docs/superpowers/specs/2026-10-30-team-demo-rules-design.md 6.5
-- (D7, D8, D9, D12; the cutover is D13).
--
-- STATUS: APPLIED 2026-11-02 13:20 UTC via MCP apply_migration (after the app's migrate-production
--   run 41448662354 put app_my_timers.entries_layout live; app main 06e5be7). VERIFIED after:
--   table empty, 3 jobs inactive, my_timers_owner no select, service_role select only, 31
--   schema_metadata rows. Manual runs (pipeline_runs all success):
--     hourly #1 1.3 s, inserted 18,147; hourly #2 0.8 s, updated 28 (the running timers), 0 ins/del;
--     nightly #1 10.6 s, inserted 151,009 (first full load, about 115 MB WAL, once);
--     nightly #2 5.7 s, updated 29, about 2 MB WAL; parity snapshot 0.8 s, 451 member-days.
--   Table: 169,156 rows, 2,059 capped (21,604 hours cut by the cap), 29 running, 5 without an
--   owner, 498 edited, 1 request, 128,576 old entries.
--   ACTIVATED 2026-11-02 13:25 UTC: all three jobs active.
--   Parity (Sep 22 to Oct 31): cap 180 days, my_timers_fix 59, running 7, unexplained 205; 203
--   of the 205 hold exact duplicate Swift entries (same start, project, task, asset) that v1's
--   cleanup counts once and My Timers keeps until the member removes one (open decision).
--
-- WHY
--   * After D13 (My Timers released, v1 retired) this table is the canonical clean timer fact
--     for reports; until then stg_timer_activities_clean stays canonical and the nightly parity
--     snapshot (stg_timer_entries_parity) is the go / no-go for the cutover.
--   * The cap (spec, verbatim): counted_minutes = least(actual_minutes, 600); for a running
--     entry actual_minutes = now - start at refresh time; is_capped = actual_minutes > 600.
--     The entry itself is never changed (Swift is never changed; the system never removes a
--     timer entry on its own); only the counted minutes stop at 600.
--   * Built here, not in the app repo: the app's migrations touch only app_my_timers (its
--     AGENTS.md rule 11). The app owns the layout (entries_layout); this file owns the table,
--     the refresh, the parity check, their grants and schedule. The app never reads this table
--     and the app's old-history refresh never reads it (no loop).
--   * Disk IO budget: the refresh builds the window in a session temp table (no WAL), writes
--     only rows that differ (IS DISTINCT FROM on every column but refreshed_at), deletes only
--     rows of its own window that the reader no longer returns, never truncates, holds an
--     advisory lock, and sets work_mem 64MB on itself (254's function-scoped pattern). Its
--     `set statement_timeout = '300s'` does not bind the call already running: a cron run is
--     bound by the postgres role default, 20 min (COST, Timeouts). A running entry is rewritten
--     on every run (its minutes count up), so a second run reports `updated` = the running
--     entries in the window, else 0.
--   * Ledger: pipeline.pipeline_runs, pipeline_name 'timer_entries_clean', 'running' then
--     'success' with records_extracted and the result in metadata. A failing run raises, so
--     pg_cron records the error in cron.job_run_details (watched by pipeline_health_watcher);
--     its pipeline_runs row rolls back with it (one transaction, no autonomous write), so a
--     'failed' row is never left. The check values are only running / success / failed.
--   * Safety: a reader that suddenly returns far fewer rows (a lost grant or policy reads as
--     empty, not as an error) would delete the window. The refresh refuses when it would delete
--     more than 10 rows and more than 5% of the window's stored rows (the app's old-history
--     refresh uses the same 5% rule); the table keeps its rows and the cron run fails loudly.
--     A real mass removal is let through by one manual run with the session setting
--     timer_entries_clean.allow_mass_delete = 'on' (see ALLOW A MASS DELETE below); the cron
--     jobs never set it.
--     The same rule guards a silent mass rewrite: a reader that loses the owners (a lost grant
--     or policy on reference.ref_employee_emails reads as empty) returns every entry with
--     emp_id null, which deletes nothing but rewrites the window unowned. The refresh refuses
--     when more than 10 stored rows and more than 5% of the window's stored rows would go from
--     an emp_id to null; the same setting lets a real one through.
--   * Minutes are never negative (check on the table). A closed entry whose end is before its
--     start would fail every run; the preflight counts such entries and refuses (0 on
--     production 2026-11-02). A running entry counts greatest(now - start, 0): a start a moment
--     ahead of the refresh clock counts 0, not a failed run.
--   * Parity key (Task 1 review): emp_id where present, else lower(user_email). A request row's
--     user_email is the member's latest email and a Swift row keeps Swift's spelling, so email
--     alone would split one person. v1 has only user_email: it maps to emp_id through
--     reference.ref_employee_emails (lower); an unmapped v1 email stays keyed by itself.
--     An entry My Timers owns nobody (emp_id null, e.g. a mixed-case Swift email) while v1
--     gives it to a member shows under cause unowned_in_my_timers.
--     A start change that moves an entry to another Manila day explains both days
--     (my_timers_fix): the new day by edited_fields, the original day (Swift's start) by the
--     change itself. A timer still running in My Timers has duration_min null in v1 (counted 0
--     there): a day whose only difference is its running entries is cause running.
--     A fix made in the old system that the app's importer could not carry (latest version of
--     an app_my_timers.legacy_import row with status no_entry, ambiguous, no_member, no_task or
--     own_addition) is in v1 but not in My Timers: its member-day (the ledger's emp_id, else
--     its lower email through ref_employee_emails; the old row's start_time, Manila) is cause
--     v1_fix_not_carried, placed after the My Timers causes and before only_in_* / unexplained.
--     carried, reverted, superseded, added_then_removed and out_of_range are not causes (in
--     both, in neither, or before the window).
--     The cause is the first that applies, so a my_timers_fix day can hide a second difference
--     on the same day (a fix plus a missing entry). The D13 review reads the minute deltas of
--     every my_timers_fix day, not only the unexplained ones.
--     Both sides drop rows with no project (v1's rule), so a request or entry with no project
--     never reads as a difference.
--   * Three pg_cron jobs: timer-entries-clean-hourly ('20 * * * *', the 40-day window),
--     timer-entries-clean-nightly ('40 19 * * *' = 3:40 AM PHT, the full range) and
--     timer-entries-clean-parity ('55 19 * * *', the snapshot). The snapshot is its own job so
--     its failure cannot roll back the nightly refresh; it takes the refresh's advisory lock
--     first, so a full run still going at 19:55 is waited for, not read half done.
--     The three jobs are created inactive: they start only after VERIFY and an IO diff of one
--     manual run of each (see ACTIVATE below).
--   * Parity window: the nightly snapshot covers the 40 Manila days ending two days ago. v1 is
--     reloaded at 13:15 PHT, so at 3:40 AM PHT yesterday's shift is not in v1 yet and today's
--     is still running; those days would differ every night for no reason.
--   * Grants: RLS on with no policy (deny-all for API roles), anon and authenticated revoked,
--     service_role read only (all revoked, then select; it bypasses RLS) like the other
--     warehouse facts. my_timers_owner gets nothing (the app never reads these tables). The
--     functions run as their owner (postgres) and are revoked from public, anon, authenticated.
--
-- COST (measured 2026-11-02 on production, read-only, before apply; plan Task 3):
--   The reader is not on production yet, so its body ran inline (parameters as literals, the
--   window check as true) under EXPLAIN (ANALYZE, BUFFERS) in a read-only transaction, as
--   postgres. Sizes: stg_timer_activities_inc 42,820 rows (3,026 pages), legacy_entry 128,576
--   (5,509 pages), timer_change 2,774, about 453 new Swift entries a day.
--   * 40-day window (hourly): 296 ms, 18,201 rows; 12,578 buffers (12,149 hit, 429 read,
--     98 MB logical, 3.4 MB from disk); its sort spilled 4.4 MB at the session work_mem, which
--     the function's 64MB keeps in memory.
--   * Full range from 2025-08-31T16:00Z (nightly): 1,452 ms, 169,136 rows; 11,033 buffers (all
--     but 10 hit, 86 MB logical); seq scans of both sources; a 36 MB sort spill (temp, no WAL).
--     Far under 120 s, so the nightly stays one call: no batching by month.
--   * v_timer_entry's window filter: stg_timer_activities_inc has no start_time index. The
--     planner walks idx_stg_timer_inc_project_start (project_did, start_time) on its second
--     column: 10,529 buffer hits for 19,069 rows, more than the 3,026-page seq scan it
--     replaces, but 87 ms. Not worth an index at this size; look again near 200k rows.
--   * Parity (v1 side): 40 days of stg_timer_activities_clean by its start_time index, 80 ms,
--     18,711 buffers. The snapshot itself writes a few hundred to a few thousand small rows.
--   * Timeouts: postgres has statement_timeout 20min in database postgres (pg_db_role_setting;
--     rolconfig has none), and every pg_cron job runs as postgres@postgres
--     (cron.use_background_workers off, so each run is a new libpq session with that default).
--     The function's `set statement_timeout = '300s'` does not rearm the timer of the call
--     already running, so 20 min is the bound that applies; the nightly needs about 2 s.
--   * Writes per day (estimate): 68 running entries now (67 of them past 10 hours, so capped
--     and still rewritten hourly as actual_minutes counts up) plus about 19 new entries an hour,
--     so about 90 rows per hourly run; at most 5 pages each with full-page images
--     (heap + 4 indexes) that is under 4 MB WAL a run, under 0.1 GB/day for 24 runs. The nightly
--     changes only what the hourly window missed (near 0) and the parity snapshot is small.
--     The first full load writes 169k rows once: about 0.15 to 0.2 GB WAL.
--   * Reads per day (estimate): the reader is about 12.6k buffers a run; the refresh around it
--     (temp table fill, the NOT EXISTS compare and the stored-window scan, about 3 PK probes a
--     row) adds about 4 to 6 times that, so roughly 0.5 to 0.7 GB logical a run, 12 to 17
--     GB/day logical over 24 runs plus about 1 GB for the nightly and parity. Almost all of it is
--     shared-buffer hits (both sources are 67 MB together and stay cached); disk reads are
--     a few MB a day.
--   * Against the Disk IO budget (OntelDB WAL is about 64 GB/day; tens of GB/day of new IO is
--     noticeable): under 0.1 GB/day WAL and a few MB/day of disk reads is noise. Recheck with a
--     pg_stat_statements diff (calls, shared_blks_read, temp_blks_written, wal_bytes) on the
--     first day after apply.
--
-- PREFLIGHT (the DO block below refuses to apply on any of these; read-only):
--   select to_regprocedure('app_my_timers.entries_layout(timestamptz,timestamptz)');   -- not null
--   select has_function_privilege('postgres', 'app_my_timers.entries_layout(timestamptz,timestamptz)', 'execute');  -- true
--     (postgres is a member of my_timers_owner, migration 280; ruling 8)
--   select has_schema_privilege('postgres', 'app_my_timers', 'usage');   -- true; granted below only if false
--   select lower(email) from reference.ref_employee_emails group by lower(email)
--     having count(distinct emp_id) > 1;                                   -- 0 rows (0 on 2026-11-02)
--     WHY: the reader keeps one row per entry; an email on two people gives the entry to one of
--     them (the one seen last), and the parity would then blame the other.
--   select pg_get_constraintdef(oid) from pg_constraint
--     where conrelid = 'pipeline.pipeline_runs'::regclass and contype = 'c';  -- running, success, failed
--   select to_regclass('data_staging.stg_timer_entries_clean'),
--          to_regclass('data_staging.stg_timer_entries_parity');            -- both null
--   select count(*) from pg_namespace where nspname = 'cron';               -- 1
--   select count(*) from cron.job where jobname like 'timer-entries-clean-%';  -- 0
--   select count(*) from app_my_timers.v_timer_entry
--     where start_time >= timestamptz '2025-08-31 16:00:00+00' and end_time < start_time;  -- 0
--   select has_table_privilege('postgres', 'app_my_timers.legacy_import', 'select');  -- true
--     (the parity cause v1_fix_not_carried reads the app's import ledger)
--
-- ROLLBACK (in this order):
--   select cron.unschedule('timer-entries-clean-hourly');
--   select cron.unschedule('timer-entries-clean-nightly');
--   select cron.unschedule('timer-entries-clean-parity');
--   drop function if exists data_staging.snapshot_timer_entries_parity();
--   drop function if exists data_staging.timer_entries_parity(date, date);
--   drop function if exists data_staging.refresh_timer_entries_clean(timestamptz, timestamptz);
--   drop table if exists data_staging.stg_timer_entries_parity;
--   drop table if exists data_staging.stg_timer_entries_clean;
--   delete from agent.schema_metadata where schema_name = 'data_staging'
--     and table_name in ('stg_timer_entries_clean', 'stg_timer_entries_parity');
--   delete from pipeline.pipeline_runs where pipeline_name = 'timer_entries_clean';  -- optional
--   (if the apply printed "granted usage on app_my_timers to postgres":
--    revoke usage on schema app_my_timers from postgres;)
--
-- VERIFY (after apply):
--   select data_staging.refresh_timer_entries_clean();
--     -- {"rows": n, "inserted": n, "updated": 0, "deleted": 0} on the first window run
--   select data_staging.refresh_timer_entries_clean();
--     -- {"inserted": 0, "deleted": 0, "updated": <running entries in the window>}
--   select count(*) from data_staging.stg_timer_entries_clean
--     where start_time >= now() - interval '40 days';
--     -- equals "rows" of the run and the reader count below (a moment apart, so +-1 at an edge)
--   select count(*) from app_my_timers.entries_layout(now() - interval '40 days', now() + interval '1 day');
--   select is_running, is_capped, count(*), max(counted_minutes) from data_staging.stg_timer_entries_clean
--     group by 1, 2;                                                       -- max counted <= 600
--   select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day');
--   select data_staging.snapshot_timer_entries_parity();                  -- rows written
--   select cause, count(*), sum(new_minutes - old_minutes) from data_staging.stg_timer_entries_parity
--     group by 1 order by 2 desc;
--   select status, records_extracted, metadata from pipeline.pipeline_runs
--     where pipeline_name = 'timer_entries_clean' order by started_at desc limit 3;
--   select jobname, schedule, command, active from cron.job where jobname like 'timer-entries-clean-%';
--     -- three rows, active false until ACTIVATE
--   select has_table_privilege('my_timers_owner', 'data_staging.stg_timer_entries_clean', 'select'),
--          has_table_privilege('my_timers_owner', 'data_staging.stg_timer_entries_parity', 'select');  -- false, false
--   select has_table_privilege('service_role', 'data_staging.stg_timer_entries_clean', 'select'),
--          has_table_privilege('service_role', 'data_staging.stg_timer_entries_clean', 'insert');  -- true, false
--
-- ACTIVATE (after VERIFY, by hand; the jobs are created inactive):
--   1. Note pg_stat_statements for the two calls (calls, total_exec_time, shared_blks_hit,
--      shared_blks_read, temp_blks_written, wal_bytes):
--        select query, calls, total_exec_time, shared_blks_hit, shared_blks_read, temp_blks_written, wal_bytes
--        from extensions.pg_stat_statements where query ilike '%timer_entries%';
--   2. Run one hourly and one nightly by hand (the job commands), then the snapshot:
--        select data_staging.refresh_timer_entries_clean();
--        select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day');
--        select data_staging.snapshot_timer_entries_parity();
--   3. Read the same pg_stat_statements rows again; the diff is one run of each. Go on only if
--      it matches COST (an hourly run about 12.6k reader buffers, WAL a few MB).
--   4. select cron.alter_job(jobid, active := true) from cron.job where jobname like 'timer-entries-clean-%';
--   Until step 4 the jobs never run, so pipeline_health_watcher (which watches them) would
--   report "no successful run found"; ship the watcher change after step 4.
--   the morning after:
--   select j.jobname, d.status, d.return_message, d.start_time from cron.job_run_details d
--     join cron.job j using (jobid) where j.jobname like 'timer-entries-clean-%'
--     order by d.runid desc limit 5;                                       -- succeeded
--
-- ALLOW A MASS DELETE OR A MASS OWNER LOSS (only after checking the change is real; one manual run):
--   begin; set local timer_entries_clean.allow_mass_delete = 'on'; select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day'); commit;
-- =============================================================================

begin;

-- 0. PREFLIGHT gates.
do $$
declare
  v_def text;
begin
  if to_regprocedure('app_my_timers.entries_layout(timestamptz,timestamptz)') is null then
    raise exception '285: app_my_timers.entries_layout(timestamptz,timestamptz) does not exist yet. Run the app''s migrate-production first.';
  end if;
  if not has_function_privilege('postgres', 'app_my_timers.entries_layout(timestamptz,timestamptz)', 'execute') then
    raise exception '285: postgres cannot execute app_my_timers.entries_layout. Check that postgres is a member of my_timers_owner (migration 280).';
  end if;
  if not has_schema_privilege('postgres', 'app_my_timers', 'usage') then
    grant usage on schema app_my_timers to postgres;
    raise notice '285: granted usage on app_my_timers to postgres';
  end if;
  if exists (
    select lower(email) from reference.ref_employee_emails
    group by lower(email)
    having count(distinct emp_id) > 1
  ) then
    raise exception '285: an email in reference.ref_employee_emails belongs to two emp_ids. One row per entry would give it to one of them. Fix the emails first.';
  end if;
  if to_regclass('pipeline.pipeline_runs') is null then
    raise exception '285: pipeline.pipeline_runs does not exist.';
  end if;
  select string_agg(pg_get_constraintdef(c.oid), ' ') into v_def
  from pg_constraint c
  where c.conrelid = to_regclass('pipeline.pipeline_runs') and c.contype = 'c';
  if v_def is null or position('''running''' in v_def) = 0 or position('''success''' in v_def) = 0
     or position('''failed''' in v_def) = 0 then
    raise exception '285: pipeline.pipeline_runs.status check is not running / success / failed: %', v_def;
  end if;
  if to_regclass('data_staging.stg_timer_entries_clean') is not null
     or to_regclass('data_staging.stg_timer_entries_parity') is not null then
    raise exception '285: a table name is taken already. Stop and look.';
  end if;
  if not exists (select 1 from pg_namespace where nspname = 'cron') then
    raise exception '285: the cron schema (pg_cron) is missing.';
  end if;
  if exists (select 1 from cron.job where jobname like 'timer-entries-clean-%') then
    raise exception '285: a timer-entries-clean-* pg_cron job exists already. Stop and look.';
  end if;
  if exists (
    select 1 from app_my_timers.v_timer_entry
    where start_time >= timestamptz '2025-08-31 16:00:00+00' and end_time < start_time
  ) then
    raise exception '285: an entry ends before it starts; its minutes would be negative and every refresh would fail. Look at it first.';
  end if;
  if to_regclass('app_my_timers.legacy_import') is null
     or not has_table_privilege('postgres', 'app_my_timers.legacy_import', 'select') then
    raise exception '285: postgres cannot read app_my_timers.legacy_import (the parity cause v1_fix_not_carried). Check the membership in my_timers_owner.';
  end if;
end $$;

-- 1. The table (Ruling 5 columns).
create table data_staging.stg_timer_entries_clean (
  swift_timer_id       text primary key,
  emp_id               text,
  user_email           text,
  project_did          text,
  project_number       integer,
  asset_did            text,
  site_name            text,
  task_did             text,
  task_clean           text,
  task_name            text,
  start_time           timestamptz not null,
  end_time             timestamptz,
  work_date            date not null,
  actual_minutes       numeric(10,2) not null check (actual_minutes >= 0),
  counted_minutes      numeric(10,2) not null check (counted_minutes >= 0 and counted_minutes <= 600),
  is_running           boolean not null,
  is_capped            boolean not null,
  source               text not null check (source in ('swift', 'legacy', 'request')),
  edited_fields        text[] not null default '{}',
  fixed_by_super_admin boolean not null default false,
  refreshed_at         timestamptz not null
);
create index idx_stg_timer_entries_clean_work_date on data_staging.stg_timer_entries_clean (work_date);
create index idx_stg_timer_entries_clean_emp_day on data_staging.stg_timer_entries_clean (emp_id, work_date);
create index idx_stg_timer_entries_clean_email_day on data_staging.stg_timer_entries_clean (lower(user_email), work_date);
-- The refresh deletes by start_time inside its window.
create index idx_stg_timer_entries_clean_start on data_staging.stg_timer_entries_clean (start_time);
alter table data_staging.stg_timer_entries_clean enable row level security;
revoke all on data_staging.stg_timer_entries_clean from anon, authenticated;
revoke all on data_staging.stg_timer_entries_clean from service_role;
grant select on data_staging.stg_timer_entries_clean to service_role;

-- 2. The parity snapshot (replaced every night). member_key = emp_id, else 'email:' || email.
create table data_staging.stg_timer_entries_parity (
  emp_id      text,
  user_email  text,
  day         date not null,
  new_minutes numeric not null,
  old_minutes numeric not null,
  cause       text not null,
  checked_at  timestamptz not null,
  member_key  text generated always as (coalesce(emp_id, 'email:' || coalesce(user_email, ''))) stored,
  primary key (member_key, day)
);
alter table data_staging.stg_timer_entries_parity enable row level security;
revoke all on data_staging.stg_timer_entries_parity from anon, authenticated;
revoke all on data_staging.stg_timer_entries_parity from service_role;
grant select on data_staging.stg_timer_entries_parity to service_role;

-- 3. The refresh. Window [p_from, p_to) on the laid start (Ruling 2); hourly 40 days back to a
--    day ahead, nightly from the first day My Timers holds (Ruling 6).
create function data_staging.refresh_timer_entries_clean(
  p_from timestamptz default now() - interval '40 days',
  p_to timestamptz default now() + interval '1 day'
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '300s'
set work_mem = '64MB'
as $$
declare
  v_now      timestamptz;
  v_run_id   uuid;
  v_rows     bigint;
  v_stored   bigint;
  v_gone     bigint;
  v_lost     bigint;
  v_inserted bigint;
  v_updated  bigint;
  v_deleted  bigint;
  v_result   jsonb;
begin
  perform pg_advisory_xact_lock(hashtext('data_staging.refresh_timer_entries_clean'));

  -- One clock for the whole run: every running entry counts up to the same instant.
  v_now := now();

  insert into pipeline.pipeline_runs (pipeline_name, started_at, status, metadata)
  values ('timer_entries_clean', v_now, 'running', jsonb_build_object('p_from', p_from, 'p_to', p_to))
  returning run_id into v_run_id;

  -- The window as the app lays it, in a session temp table (no WAL). The reader refuses an
  -- empty, reversed or too early window with 22023.
  if to_regclass('pg_temp._tec') is not null then
    drop table pg_temp._tec;
  end if;
  create temp table pg_temp._tec
    (like data_staging.stg_timer_entries_clean including defaults) on commit drop;

  insert into pg_temp._tec (
    swift_timer_id, emp_id, user_email, project_did, project_number, asset_did, site_name,
    task_did, task_clean, task_name, start_time, end_time, work_date, actual_minutes,
    counted_minutes, is_running, is_capped, source, edited_fields, fixed_by_super_admin,
    refreshed_at
  )
  select l.swift_timer_id, l.emp_id, l.user_email, l.project_did, l.project_number, l.asset_did,
         l.site_name, l.task_did, l.task_clean, l.task_name, l.start_time, l.end_time,
         (l.start_time at time zone 'Asia/Manila')::date,
         m.actual_minutes,
         least(m.actual_minutes, 600),
         l.end_time is null,
         m.actual_minutes > 600,
         l.source, coalesce(l.edited_fields, '{}'::text[]), coalesce(l.fixed_by_super_admin, false),
         v_now
  from app_my_timers.entries_layout(p_from, p_to) l
  cross join lateral (
    -- A running entry counts up to v_now, never below 0; a closed one is not clamped (an end
    -- before its start fails the table check, loudly).
    select round(extract(epoch from
             case when l.end_time is null then greatest(v_now - l.start_time, interval '0')
                  else l.end_time - l.start_time end) / 60, 2) as actual_minutes
  ) m;
  get diagnostics v_rows = row_count;

  -- One row per entry. Two rows with one id fail the run here, before any write.
  create unique index on pg_temp._tec (swift_timer_id);
  analyze pg_temp._tec;

  -- Refuse a reader that suddenly returns far fewer rows, or far fewer owners (see WHY, Safety).
  select count(*),
         count(*) filter (where not exists (
           select 1 from pg_temp._tec t where t.swift_timer_id = s.swift_timer_id)),
         count(*) filter (where s.emp_id is not null
           and exists (
             select 1 from pg_temp._tec t
             where t.swift_timer_id = s.swift_timer_id and t.emp_id is null))
    into v_stored, v_gone, v_lost
  from data_staging.stg_timer_entries_clean s
  where s.start_time >= p_from and s.start_time < p_to;
  if v_lost > 10 and v_lost > v_stored * 0.05
     and coalesce(current_setting('timer_entries_clean.allow_mass_delete', true), '') <> 'on' then
    raise exception 'refresh_timer_entries_clean: % of the % stored entries of the window would lose their owner (emp_id becomes null; more than 5%%). Refused; the table keeps its rows. Check entries_layout and the grants on reference.ref_employee_emails and reference.ref_swift_users; if the change is real, run once with set local timer_entries_clean.allow_mass_delete = on (header of migration 285).',
      v_lost, v_stored;
  end if;
  if v_gone > 10 and v_gone > v_stored * 0.05
     and coalesce(current_setting('timer_entries_clean.allow_mass_delete', true), '') <> 'on' then
    raise exception 'refresh_timer_entries_clean: the reader would remove % of the % stored entries of the window (more than 5%%). Refused; the table keeps its rows. Check entries_layout and its grants; if the removals are real, run once with set local timer_entries_clean.allow_mass_delete = on (header of migration 285).',
      v_gone, v_stored;
  end if;

  -- Write only what differs: new entries, and stored ones whose content changed.
  with up as (
    insert into data_staging.stg_timer_entries_clean as s (
      swift_timer_id, emp_id, user_email, project_did, project_number, asset_did, site_name,
      task_did, task_clean, task_name, start_time, end_time, work_date, actual_minutes,
      counted_minutes, is_running, is_capped, source, edited_fields, fixed_by_super_admin,
      refreshed_at
    )
    select t.swift_timer_id, t.emp_id, t.user_email, t.project_did, t.project_number, t.asset_did,
           t.site_name, t.task_did, t.task_clean, t.task_name, t.start_time, t.end_time,
           t.work_date, t.actual_minutes, t.counted_minutes, t.is_running, t.is_capped, t.source,
           t.edited_fields, t.fixed_by_super_admin, t.refreshed_at
    from pg_temp._tec t
    where not exists (
      select 1 from data_staging.stg_timer_entries_clean c
      where c.swift_timer_id = t.swift_timer_id
        and (c.emp_id, c.user_email, c.project_did, c.project_number, c.asset_did, c.site_name, c.task_did, c.task_clean, c.task_name, c.start_time, c.end_time, c.work_date, c.actual_minutes, c.counted_minutes, c.is_running, c.is_capped, c.source, c.edited_fields, c.fixed_by_super_admin) is not distinct from
            (t.emp_id, t.user_email, t.project_did, t.project_number, t.asset_did, t.site_name, t.task_did, t.task_clean, t.task_name, t.start_time, t.end_time, t.work_date, t.actual_minutes, t.counted_minutes, t.is_running, t.is_capped, t.source, t.edited_fields, t.fixed_by_super_admin)
    )
    on conflict (swift_timer_id) do update set
      emp_id = excluded.emp_id,
      user_email = excluded.user_email,
      project_did = excluded.project_did,
      project_number = excluded.project_number,
      asset_did = excluded.asset_did,
      site_name = excluded.site_name,
      task_did = excluded.task_did,
      task_clean = excluded.task_clean,
      task_name = excluded.task_name,
      start_time = excluded.start_time,
      end_time = excluded.end_time,
      work_date = excluded.work_date,
      actual_minutes = excluded.actual_minutes,
      counted_minutes = excluded.counted_minutes,
      is_running = excluded.is_running,
      is_capped = excluded.is_capped,
      source = excluded.source,
      edited_fields = excluded.edited_fields,
      fixed_by_super_admin = excluded.fixed_by_super_admin,
      refreshed_at = excluded.refreshed_at
    returning (xmax = 0) as is_new
  )
  select count(*) filter (where is_new), count(*) filter (where not is_new)
    into v_inserted, v_updated
  from up;

  -- Rows of this window the reader no longer returns: removed, moved out, request withdrawn.
  -- Only the window's own rows: a stored start outside [p_from, p_to) is never touched.
  delete from data_staging.stg_timer_entries_clean c
  where c.start_time >= p_from and c.start_time < p_to
    and not exists (select 1 from pg_temp._tec t where t.swift_timer_id = c.swift_timer_id);
  get diagnostics v_deleted = row_count;

  v_result := jsonb_build_object('inserted', v_inserted, 'updated', v_updated,
                                 'deleted', v_deleted, 'rows', v_rows);
  update pipeline.pipeline_runs
     set status = 'success',
         completed_at = clock_timestamp(),
         records_extracted = v_rows::integer,
         metadata = metadata || v_result
   where run_id = v_run_id;
  return v_result;
end
$$;
revoke all on function data_staging.refresh_timer_entries_clean(timestamptz, timestamptz)
  from public, anon, authenticated;

-- 4. The parity check (Ruling 7), read only. Days p_from to p_to inclusive (Manila).
--    Key: emp_id, else 'email:' || lower(email). A day differs when |new - old| > 1 minute.
--    Cause, first that applies: cap (the day has a capped entry and the uncapped sum matches
--    v1), running (the day has a running entry and its closed entries match v1, where a running
--    timer has no duration), my_timers_fix (an entry of the day has edited_fields, is a
--    request, was removed in My Timers, or had its start moved away from this day),
--    unowned_in_my_timers (an entry of the day has no owner in My Timers while
--    v1's email maps to a member), v1_fix_not_carried (an old-system fix the importer could not
--    carry is on the day), only_in_my_timers / only_in_v1 (no rows on one side), else
--    unexplained. Rows with no project are left out on both sides.
create function data_staging.timer_entries_parity(p_from date, p_to date)
returns table (emp_id text, user_email text, day date, new_minutes numeric, old_minutes numeric, cause text)
language sql
stable
security definer
set search_path = ''
set statement_timeout = '300s'
set work_mem = '64MB'
as $$
  with bounds as materialized (
    select (greatest(p_from, date '2025-09-01')::timestamp at time zone 'Asia/Manila') as t_from,
           ((p_to + 1)::timestamp at time zone 'Asia/Manila') as t_to
  ),
  -- One owner per lower email (the preflight guarantees one; the order matches the reader).
  owners as materialized (
    select distinct on (lower(e.email)) lower(e.email) as email, e.emp_id
    from reference.ref_employee_emails e
    order by lower(e.email), e.last_seen desc, e.emp_id
  ),
  -- The email a member's row shows (the one seen last).
  shown as materialized (
    select distinct on (e.emp_id) e.emp_id, lower(e.email) as email
    from reference.ref_employee_emails e
    order by e.emp_id, e.last_seen desc, e.email
  ),
  new_rows as (
    select coalesce(n.emp_id, 'email:' || coalesce(lower(n.user_email), '')) as member_key,
           n.work_date as day, n.counted_minutes, n.actual_minutes, n.is_capped, n.is_running,
           (cardinality(n.edited_fields) > 0 or n.source = 'request') as fixed,
           case when n.emp_id is null then o.emp_id end as unowned_emp
    from data_staging.stg_timer_entries_clean n
    cross join bounds b
    left join owners o on o.email = lower(n.user_email)
    where n.project_did is not null
      and n.start_time >= b.t_from and n.start_time < b.t_to
  ),
  new_days as (
    select r.member_key, r.day, sum(r.counted_minutes) as minutes,
           sum(r.actual_minutes) as uncapped, bool_or(r.is_capped) as capped,
           coalesce(sum(r.counted_minutes) filter (where not r.is_running), 0) as closed,
           bool_or(r.is_running) as running,
           bool_or(r.fixed) as fixed, bool_or(r.unowned_emp is not null) as unowned
    from new_rows r
    group by r.member_key, r.day
  ),
  -- The member v1 gives an unowned entry to: that member's day is explained too.
  unowned_days as (
    select distinct r.unowned_emp as member_key, r.day
    from new_rows r
    where r.unowned_emp is not null
  ),
  old_days as (
    select coalesce(o.emp_id, 'email:' || lower(c.user_email)) as member_key,
           (c.start_time at time zone 'Asia/Manila')::date as day,
           sum(coalesce(c.duration_min, 0)) as minutes
    from data_staging.stg_timer_activities_clean c
    cross join bounds b
    left join owners o on o.email = lower(c.user_email)
    where c.project_did is not null and c.user_email is not null
      and c.start_time >= b.t_from and c.start_time < b.t_to
    group by 1, 2
  ),
  -- Days a My Timers change took minutes from: an entry removed (latest remove / restore is a
  -- remove), on its laid day; and an entry whose start was moved, on Swift's original day
  -- (the new day is explained by the entry's edited_fields).
  changes as materialized (
    select s.emp_id, s.last_kind, s.new_start, t.start_time
    from (
      select c.emp_id, c.swift_timer_id,
             (array_agg(c.kind order by c.id desc) filter (where c.kind in ('remove', 'restore')))[1] as last_kind,
             (array_agg(c.new_start order by c.id desc) filter (where 'start' = any (c.fields)))[1] as new_start
      from app_my_timers.v_timer_change_seen c
      group by c.emp_id, c.swift_timer_id
    ) s
    join app_my_timers.v_timer_entry t on t.swift_timer_id = s.swift_timer_id
    where s.last_kind = 'remove' or s.new_start is not null
  ),
  fixed_days as (
    select c.emp_id as member_key,
           (coalesce(c.new_start, c.start_time) at time zone 'Asia/Manila')::date as day
    from changes c
    where c.last_kind = 'remove'
    union
    select c.emp_id, (c.start_time at time zone 'Asia/Manila')::date
    from changes c
    where c.new_start is not null
  ),
  -- Old-system fixes the app's importer could not carry (latest version of each old row): in
  -- v1, not in My Timers. Keyed like old_days: the ledger's emp_id, else the lower email.
  not_carried as (
    select distinct coalesce(x.emp_id, o.emp_id, 'email:' || lower(x.user_email)) as member_key,
           x.day
    from (
      select distinct on (l.source, l.source_id)
             l.status, l.emp_id, l.user_email,
             ((l.old_row ->> 'start_time')::timestamptz at time zone 'Asia/Manila')::date as day
      from app_my_timers.legacy_import l
      order by l.source, l.source_id, l.source_version desc, l.id desc
    ) x
    left join owners o on o.email = lower(x.user_email)
    where x.status in ('no_entry', 'ambiguous', 'no_member', 'no_task', 'own_addition')
      and x.day is not null
  ),
  diff as (
    select coalesce(n.member_key, o.member_key) as member_key,
           coalesce(n.day, o.day) as day,
           round(coalesce(n.minutes, 0), 2) as new_minutes,
           round(coalesce(o.minutes, 0)::numeric, 2) as old_minutes,
           n.member_key is not null as has_new,
           o.member_key is not null as has_old,
           coalesce(n.capped, false)
             and abs(coalesce(n.uncapped, 0) - coalesce(o.minutes, 0)) <= 1 as cap_explains,
           coalesce(n.running, false)
             and abs(coalesce(n.closed, 0) - coalesce(o.minutes, 0)) <= 1 as running_explains,
           coalesce(n.fixed, false) as fixed,
           coalesce(n.unowned, false) as unowned
    from new_days n
    full join old_days o on o.member_key = n.member_key and o.day = n.day
    where abs(coalesce(n.minutes, 0) - coalesce(o.minutes, 0)) > 1
  )
  select case when d.member_key like 'email:%' then null else d.member_key end,
         case when d.member_key like 'email:%' then nullif(substr(d.member_key, 7), '')
              else sh.email end,
         d.day, d.new_minutes, d.old_minutes,
         case
           when d.cap_explains then 'cap'
           when d.running_explains then 'running'
           when d.fixed or r.member_key is not null then 'my_timers_fix'
           when d.unowned or u.member_key is not null then 'unowned_in_my_timers'
           when nc.member_key is not null then 'v1_fix_not_carried'
           when not d.has_old then 'only_in_my_timers'
           when not d.has_new then 'only_in_v1'
           else 'unexplained'
         end
  from diff d
  left join shown sh on sh.emp_id = d.member_key
  left join fixed_days r on r.member_key = d.member_key and r.day = d.day
  left join unowned_days u on u.member_key = d.member_key and u.day = d.day
  left join not_carried nc on nc.member_key = d.member_key and nc.day = d.day
  order by 2, 3
$$;
revoke all on function data_staging.timer_entries_parity(date, date)
  from public, anon, authenticated;

-- 5. The nightly snapshot: the 40 Manila days ending two days ago (see WHY, Parity window).
create function data_staging.snapshot_timer_entries_parity()
returns integer
language plpgsql
security definer
set search_path = ''
set statement_timeout = '300s'
set work_mem = '64MB'
as $$
declare
  v_today date := (statement_timestamp() at time zone 'Asia/Manila')::date;
  v_rows  integer;
begin
  perform pg_advisory_xact_lock(hashtext('data_staging.snapshot_timer_entries_parity'));
  -- Wait for a refresh still running (the nightly full run): read its result, not its start.
  perform pg_advisory_xact_lock(hashtext('data_staging.refresh_timer_entries_clean'));
  delete from data_staging.stg_timer_entries_parity;
  insert into data_staging.stg_timer_entries_parity
    (emp_id, user_email, day, new_minutes, old_minutes, cause, checked_at)
  select p.emp_id, p.user_email, p.day, p.new_minutes, p.old_minutes, p.cause, statement_timestamp()
  from data_staging.timer_entries_parity(v_today - 41, v_today - 2) p;
  get diagnostics v_rows = row_count;
  return v_rows;
end
$$;
revoke all on function data_staging.snapshot_timer_entries_parity()
  from public, anon, authenticated;

-- 6. The schedule. Each command only calls one of the two functions.
--    Hourly at :20, the 40-day window. Nightly 19:40 UTC = 3:40 AM PHT, the full range.
--    19:55 UTC, the parity snapshot, its own job (a failure cannot roll back the refresh).
--    The advisory locks queue an overlap.
--    Every job is created inactive (cron.schedule returns its jobid); ACTIVATE in the header
--    turns them on after VERIFY and the IO diff of one manual run.
select cron.alter_job(cron.schedule(
  'timer-entries-clean-hourly',
  '20 * * * *',
  $cmd$select data_staging.refresh_timer_entries_clean()$cmd$
), active := false);
select cron.alter_job(cron.schedule(
  'timer-entries-clean-nightly',
  '40 19 * * *',
  $cmd$select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day')$cmd$
), active := false);
select cron.alter_job(cron.schedule(
  'timer-entries-clean-parity',
  '55 19 * * *',
  $cmd$select data_staging.snapshot_timer_entries_parity()$cmd$
), active := false);

-- 7. The record for the AI semantic layer: a row per table and per column.
insert into agent.schema_metadata
  (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
select v.schema_name, v.table_name, v.column_name, v.description, v.business_context, v.data_notes, v.related_tables
from (values
  ('data_staging', 'stg_timer_entries_clean', null::text,
   'One row per timer entry as the My Timers app lays it out for every member: Swift and old entries with every fix laid on, removed entries left out, approved missing entries added. counted_minutes is capped at 600 (10 hours).',
   'Canonical clean timer fact after the My Timers release (decision D13); until then data_staging.stg_timer_activities_clean is canonical and stg_timer_entries_parity compares the two every night. Count hours with counted_minutes; actual_minutes is the uncapped clock time. Group by emp_id, else lower(user_email): an entry no member owns carries emp_id null and would lose its hours in a group by emp_id alone.',
   'Owner: My Timers. Grain: one row per timer entry (swift_timer_id). Source: data_staging.refresh_timer_entries_clean() from app_my_timers.entries_layout: hourly (pg_cron timer-entries-clean-hourly, last 40 days) and nightly (timer-entries-clean-nightly, from 2025-09-01 Manila); the jobs are created inactive until ACTIVATE (migration 285 header). Cap: counted_minutes = least(actual_minutes, 600); a running entry counts up to the refresh time. Writes only changed rows. The My Timers app never reads this table. PII: yes, user_email (employee email). Retention: kept as long as data_staging.stg_timer_activities_clean (no purge of its own).',
   array['app_my_timers.entries_layout', 'data_staging.stg_timer_activities_clean', 'data_staging.stg_timer_entries_parity']::text[]),
  ('data_staging', 'stg_timer_entries_parity', null::text,
   'Member-days where stg_timer_entries_clean (counted minutes) and v1 stg_timer_activities_clean (duration_min) differ by more than one minute, with the cause.',
   'The go / no-go for moving reports to stg_timer_entries_clean at the My Timers release (D13). Read only; replaced every night. unexplained and unowned_in_my_timers days block the D13 cutover until explained; v1_fix_not_carried days need the old fix carried or accepted; my_timers_fix days can hide a second difference, so read their minute deltas too.',
   'Owner: My Timers. Grain: one row per member-day that differs. Source: data_staging.snapshot_timer_entries_parity() (pg_cron timer-entries-clean-parity, 19:55 UTC, inactive until ACTIVATE) after the nightly full refresh, for the 40 Manila days ending two days ago (v1 loads at 13:15 PHT). Key: emp_id, else the lower email (member_key). Causes: cap, running, my_timers_fix, unowned_in_my_timers, v1_fix_not_carried, only_in_my_timers, only_in_v1, unexplained. PII: yes, user_email (employee email). Retention: the last snapshot only (replaced every night).',
   array['data_staging.stg_timer_entries_clean', 'data_staging.stg_timer_activities_clean', 'reference.ref_employee_emails']::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'swift_timer_id', 'Entry id (PK): the Swift timer id; old-<hash> for an entry of the old system; req-<id> for an approved missing entry.', 'One row per entry.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'emp_id', 'The member who owns the entry in My Timers.', 'Join to the employee directory.', 'Null when no member owns the entry (email unknown or in another letter case and the Swift user unknown); such an entry is kept unchanged.', array['reference.ref_employee_emails']::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'user_email', 'Email on the entry: Swift''s spelling for Swift and old rows, the member''s latest email for a request row.', 'Group by emp_id, else lower(user_email) (an entry no member owns has emp_id null).', 'PII: employee email.', array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'project_did', 'Swift project id, after a work change.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'project_number', 'Swift project number, after a work change.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'asset_did', 'Swift asset id, after a work change.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'site_name', 'Site name, after a work change.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'task_did', 'Swift task id, after a work change.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'task_clean', 'Clean grouping name of the task (no number prefix), as My Timers groups it.', 'Use for groupings and charts.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'task_name', 'Swift''s own task name.', 'Show on per-entry displays.', 'Null for a request row.', array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'start_time', 'Start, after an approved start change (UTC).', 'Convert to America/New_York or Asia/Manila for display.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'end_time', 'End, after a change (UTC). Null while the timer runs.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'work_date', 'Manila date of start_time.', 'The day reports group by.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'actual_minutes', 'Clock minutes from start to end, or to the refresh time while running. Not capped.', 'Do not sum for hours worked; use counted_minutes.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'counted_minutes', 'least(actual_minutes, 600): the minutes that count.', 'Sum this for hours.', 'Never above 600 (check constraint).', array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'is_running', 'True while the timer has no end.', null, 'Its minutes grow at every refresh.', array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'is_capped', 'True when actual_minutes is above 600.', 'A running timer is not capped before 10 hours.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'source', 'swift, legacy (an entry of the old system) or request (an approved missing entry).', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'edited_fields', 'Fields changed in My Timers: start, end, work. Empty when unchanged.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'fixed_by_super_admin', 'True when a super admin changed the entry or added the request in Fix as.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_clean', 'refreshed_at', 'When the refresh last wrote the row.', 'Rows that did not change keep their time.', null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'emp_id', 'Member of the day; null when the email maps to no member.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'user_email', 'The member''s latest email, or the unmapped email itself.', null, 'PII: employee email.', array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'day', 'Manila date.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'new_minutes', 'Sum of counted_minutes in stg_timer_entries_clean.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'old_minutes', 'Sum of duration_min in stg_timer_activities_clean (rows with a project and an email).', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'cause', 'Why the day differs, the first that applies: cap, running, my_timers_fix, unowned_in_my_timers, v1_fix_not_carried, only_in_my_timers, only_in_v1, unexplained.', 'unexplained and unowned_in_my_timers days block the D13 cutover until explained.', 'A my_timers_fix day can hide a second difference: read new_minutes - old_minutes.', array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'checked_at', 'When the snapshot was taken.', null, null, array[]::text[]),
  ('data_staging', 'stg_timer_entries_parity', 'member_key', 'emp_id, else email: plus the email (PK with day).', null, 'Generated column.', array[]::text[])
) as v(schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
where not exists (
  select 1 from agent.schema_metadata m
  where m.schema_name = v.schema_name and m.table_name = v.table_name
    and m.column_name is not distinct from v.column_name
);

commit;
