-- =============================================================================
-- 288_timer_clean_from_my_timers.sql
-- Retire the old timer system: the old clean table is refilled from My Timers.
--
-- data_staging.stg_timer_activities_clean is what every report reads (DRMC hours via
-- analytics.mv_timer_day_rollup -> mv_hr_report_review / v_daily_report_approvals, DRMC report
-- detail and member week, revenue mv_timer_revenue(+_daily), v_timer_task_mix_daily,
-- member_weekly_task_mix, gap_report.mv_timer_base, the health and roster-gap watchers and the
-- clean export to Drive). Until now rebuild_timer_clean() built it from the raw extract
-- (stg_timer_activities) plus the old email forms (app_timer.corrections, entry_removals,
-- entry_additions, duplicate_reviews). From this migration it is built from
-- data_staging.stg_timer_entries_clean (migrations 285/286): My Timers' answer, with the
-- members' own fixes, the 10-hour cap and exact duplicates counted once.
-- Same table, same columns, same function name: no reader changes, no gap_report object touched.
--
-- STATUS: APPLIED 2026-11-04 ~9:10 PM ET via Supabase MCP apply_migration (PR #124, 0966993),
--   after ontel-my-timers' legacy-import workflow was disabled (gh workflow disable, same night).
--   VERIFIED: first run wrote 169,914 rows (run_id ...0003) and left the 257,511 older rows;
--   427,625 -> 427,425 rows (the 45 rows with no project/email and the dropped duplicates left);
--   0 old-system rows since the cutoff; max duration 600. Two more runs rewrote 0 rows.
--   October after refreshing the MVs: DRMC rollup 14,418 -> 14,216 h (1,613 -> 1,614 member-days),
--   revenue $461,325 -> $460,660, task mix 15,146 -> 14,787 h. Cron job 55 at :35.
--
-- WHY (agreed 2026-11-04)
--   * The Timer Activity Entries email is retired (PR #123); members fix timers in My Timers.
--     My Timers' fixes reached no report: they lived only in stg_timer_entries_clean, which had
--     no readers. Over 2026-10-05..11-03 the two tables differed on 220 member-days, all
--     explained (my_timers_fix -197 h, cap -158 h, duplicate -15 h, running +20 h, only in
--     My Timers +10 h; the 3 "unexplained" days were Swift end times edited after the old
--     extract copied them, so My Timers is the right one).
--   * Refill the old table instead of moving each reader (one migration, no DRMC code change,
--     gap_report keeps its input and gains the fixes).
--   * Reports use the capped minutes, like My Timers. A capped entry's END is also moved to
--     start + 600 minutes, because the hours rollup (mv_timer_day_rollup) measures start to end.
--   * Switch as soon as parity passes (not held for the Nov 5 member pack).
--
-- WHAT EACH ROW IS (only rows starting on or after 2025-08-31 16:00 UTC = 2025-09-01 PHT)
--   * One row per My Timers entry that is not an exact duplicate (is_duplicate false).
--   * id = 2^61 + the first 60 bits of md5(swift_timer_id): stable per entry, above every old
--     serial id (max 10,000,000,000,386 today), so a row keeps its id across runs and edits.
--   * project, project_number, project_did, site_name, task (= Swift's task name), task_clean,
--     asset_did, user_email, start_time from My Timers.
--   * end_time NULL and duration_min 0 while running (the old table's shape); a capped entry
--     ends at start + 600 minutes with 600 minutes; otherwise My Timers' end and minutes.
--   * site_id, site and user GPS, user_name, user_role (My Timers does not carry them): from
--     Swift's live copy (stg_timer_activities_inc) for a Swift entry; else from the row this
--     function wrote last time (same id); else, on the first run, from the old row the legacy
--     entry was copied from; else the latest live row of the member. site_id and the site GPS
--     are dropped when the member moved the entry to another site; the project name is taken
--     only from a copy of the same project, else from data_staging.stg_projects.
--   * start_date / end_date = the first and last day of the start's ET month, run_date = the
--     start's ET date (the old table's start_date/end_date were the extraction month, and
--     mv_timer_revenue reads them as first/last work date). run_id = ...0003 marks these rows.
--   * Rows before the cutoff (257,511 today, back to 2023-01-02) are never touched: they stay
--     as the old system left them.
--   * The 45 rows since the cutoff with no project or no email leave (My Timers never had them).
--
-- APPLY ORDER (a hard dependency, found in review 2026-11-04)
--   ontel-my-timers' nightly workflow legacy-import (07:00 UTC, scripts/legacy-import.ts ->
--   app_my_timers.legacy_refresh_entries() + legacy_import_changes()) copies the OLD rows of
--   this table into app_my_timers.legacy_entry, keyed on project|email|start|site|task|twin.
--   After this migration those rows are My Timers' own output, so the copy would loop: a
--   member-moved start becomes a second entry (the first one vanishes with its fixes), a dropped
--   duplicate vanishes, and the cap is written back into legacy_entry.duration_min.
--   So, before applying: (1) the old-system seeding into My Timers is finished; (2) the
--   legacy-import workflow is disabled (gh workflow disable legacy-import.yml -R Ontel/ontel-my-timers).
--   The old forms are closed with this switch, so legacy_import_changes has nothing new to import.
--
-- SAFETY
--   * Refuses (raises, writes nothing) when My Timers' table has no entries, or fewer than 90%
--     of the rows the reports hold since the cutoff, or fewer than 90% of them in the last 40
--     days (My Timers' hourly window): a broken refresh can never empty a report.
--   * Builds in a temp table, writes only the rows that differ (WAL diet, migration 257), never
--     truncates (readers stay up), advisory lock first (migration 259).
--   * The old email forms stop affecting reports at apply: their corrections only ever reached
--     this table through the old body. Close the two forms right after (INIT-862).
--
-- PREFLIGHT (run 2026-11-04 ~9 PM ET, read-only)
--   * Target query (the select below) on production: 169,914 rows, 169,914 distinct ids,
--     8.0 s (EXPLAIN ANALYZE), function statement_timeout 300 s.
--   * Every source = 'swift' entry has its stg_timer_activities_inc row, every 'legacy' entry its
--     app_my_timers.legacy_entry row (0 missing each).
--   * pg_depend readers of the table: the five listed above (unchanged, they keep reading it).
--   * Next free migration number listed: 288 (287 = my_timers_shift_reminder_cron).
--   * Runs every hour at :35, after My Timers' hourly refresh at :20 (cron 51) and its nightly
--     full refresh at 19:40 UTC (cron 52). The rollup refreshes every 10 minutes (cron 11).
--     The workflows that call rebuild_timer_clean() (--apply) now just run this body too.
--
-- ROLLBACK
--   Re-run the 259 body of data_staging.rebuild_timer_clean()
--   (swift_api_pipeline/migrations/259_rebuild_timer_clean_advisory_lock.sql, function only),
--   then `select data_staging.rebuild_timer_clean();` (it removes the ...0003 rows and puts the
--   old rows back from stg_timer_activities and the forms), then
--   `select cron.unschedule('rebuild-timer-clean-hourly');`.
--
-- VERIFY (after apply)
--   1. select data_staging.rebuild_timer_clean();  -- notice: rows in target, deleted, inserted
--   2. select count(*), count(*) filter (where run_id = '00000000-0000-0000-0000-000000000003')
--      from data_staging.stg_timer_activities_clean;   -- 257,511 old + ~169,9xx new
--   3. Day totals per member, last 30 days: mv_timer_day_rollup union_min vs My Timers' clock
--      hours (refresh the rollup first); revenue totals before/after.
--   4. Second call writes ~0 rows (only entries that changed since).
--   5. cron.job_run_details for rebuild-timer-clean-hourly ordered by runid desc: succeeded.
-- =============================================================================

do $preflight$
begin
  if to_regclass('data_staging.stg_timer_entries_clean') is null then
    raise exception '288 preflight: data_staging.stg_timer_entries_clean is missing (migration 285)';
  end if;
  if not exists (select 1 from data_staging.stg_timer_entries_clean) then
    raise exception '288 preflight: data_staging.stg_timer_entries_clean is empty';
  end if;
end
$preflight$;

create or replace function data_staging.rebuild_timer_clean()
returns void
language plpgsql
set statement_timeout = '300s'
set search_path = ''
as $function$
declare
  c_cutoff constant timestamptz := timestamptz '2025-08-31 16:00:00+00';
  c_run_id constant uuid := '00000000-0000-0000-0000-000000000003'::uuid;
  v_src      bigint;
  v_cur      bigint;
  v_src_40   bigint;
  v_cur_40   bigint;
  v_deleted  bigint;
  v_inserted bigint;
begin
  perform pg_advisory_xact_lock(hashtext('data_staging.rebuild_timer_clean'));

  -- Refuse before any write: an empty or short My Timers table must never empty the reports.
  select count(*) into v_src from data_staging.stg_timer_entries_clean n where not n.is_duplicate;
  select count(*) into v_cur from data_staging.stg_timer_activities_clean c
  where c.start_time >= c_cutoff;
  if v_src = 0 or v_src < 0.9 * v_cur then
    raise exception 'rebuild_timer_clean: My Timers has % entries but the reports hold % since 2025-09-01; refusing to shrink them by more than 10 percent', v_src, v_cur;
  end if;
  -- The same check on the last 40 days alone (My Timers' hourly window): a loss inside that
  -- window is too small for the whole-range check to see.
  select count(*) into v_src_40 from data_staging.stg_timer_entries_clean n
  where not n.is_duplicate and n.start_time >= now() - interval '40 days';
  select count(*) into v_cur_40 from data_staging.stg_timer_activities_clean c
  where c.start_time >= now() - interval '40 days';
  if v_src_40 < 0.9 * v_cur_40 then
    raise exception 'rebuild_timer_clean: My Timers has % entries in the last 40 days but the reports hold %; refusing to shrink them by more than 10 percent', v_src_40, v_cur_40;
  end if;

  create temp table if not exists tmp_timer_clean
    (like data_staging.stg_timer_activities_clean including defaults) on commit drop;
  truncate pg_temp.tmp_timer_clean;

  insert into pg_temp.tmp_timer_clean (
    id, project, project_number, project_did, site_name, site_id, task, task_clean,
    site_lat, site_long, user_lat, user_long, user_accuracy_m, site_vs_user_km,
    start_time, end_time, duration_min, user_name, user_email, user_role,
    run_id, run_date, loaded_at, start_date, end_date, asset_did
  )
  select
    k.id,
    -- A name only from a copy of the same project (a member may have moved the entry's work).
    coalesce(case when i.project_did = n.project_did then i.project end,
             case when le.project_did = n.project_did then le.project end,
             case when cur.project_did = n.project_did then cur.project end,
             case when le.project_did = n.project_did then fl.project end,
             px.project),
    n.project_number, n.project_did, n.site_name,
    case when i.swift_timer_id is not null then
           case when i.site_name is not distinct from n.site_name then i.site_id end
         when le.swift_timer_id is not null then
           case when le.site_name is not distinct from n.site_name then le.site_id end
         else cur.site_id end,
    n.task_name, n.task_clean,
    case when s.same_site then coalesce(i.site_lat, cur.site_lat, fl.site_lat) end,
    case when s.same_site then coalesce(i.site_long, cur.site_long, fl.site_long) end,
    coalesce(i.user_lat, cur.user_lat, fl.user_lat),
    coalesce(i.user_long, cur.user_long, fl.user_long),
    coalesce(i.user_accuracy_m, cur.user_accuracy_m, fl.user_accuracy_m),
    case when s.same_site then coalesce(i.site_vs_user_km, cur.site_vs_user_km, fl.site_vs_user_km) end,
    n.start_time,
    case when n.is_running then null
         when n.is_capped then n.start_time + interval '600 minutes'
         else n.end_time end,
    case when n.is_running then 0 else n.counted_minutes end,
    coalesce(i.user_name, le.user_name, cur.user_name, fl.user_name, nm.user_name),
    n.user_email,
    coalesce(i.user_role, cur.user_role, fl.user_role, nm.user_role),
    c_run_id,
    (n.start_time at time zone 'America/New_York')::date,
    now(),
    date_trunc('month', n.start_time at time zone 'America/New_York')::date,
    (date_trunc('month', n.start_time at time zone 'America/New_York') + interval '1 month - 1 day')::date,
    n.asset_did
  from data_staging.stg_timer_entries_clean n
  cross join lateral (
    select (('x' || substr(md5(n.swift_timer_id), 1, 15))::bit(60)::bigint) + 2305843009213693952 as id
  ) k
  left join data_staging.stg_timer_activities_inc i on i.swift_timer_id = n.swift_timer_id
  left join app_my_timers.legacy_entry le on le.swift_timer_id = n.swift_timer_id
  left join data_staging.stg_timer_activities_clean cur on cur.id = k.id
  -- First run only: the old row a legacy entry was copied from (its original key).
  left join lateral (
    select c.project, c.site_lat, c.site_long, c.user_lat, c.user_long, c.user_accuracy_m,
           c.site_vs_user_km, c.user_name, c.user_role
    from data_staging.stg_timer_activities_clean c
    where cur.id is null and le.swift_timer_id is not null
      and c.user_email = le.user_email and c.start_time = le.start_time
      and c.project_did = le.project_did
      and c.site_name is not distinct from le.site_name
      and c.site_id is not distinct from le.site_id
      and c.task is not distinct from le.task
      and c.run_id is distinct from c_run_id
    order by c.id
    limit 1
  ) fl on true
  -- The project's name when no copy of the same project has it (an added entry, or a member
  -- moved the entry to another project).
  left join lateral (
    select p.project_name as project from data_staging.stg_projects p
    where coalesce(i.project_did = n.project_did, false) is false
      and coalesce(le.project_did = n.project_did, false) is false
      and coalesce(cur.project_did = n.project_did, false) is false
      and p.project_did = n.project_did
    limit 1
  ) px on true
  -- An entry with no Swift, legacy or earlier row (a member's added entry): latest live names.
  left join lateral (
    select x.user_name, x.user_role from data_staging.stg_timer_activities_inc x
    where i.swift_timer_id is null and le.swift_timer_id is null and cur.id is null
      and lower(x.user_email) = lower(n.user_email)
    order by x.start_time desc
    limit 1
  ) nm on true
  cross join lateral (
    select coalesce(i.site_name, le.site_name, cur.site_name) is not distinct from n.site_name as same_site
  ) s
  where not n.is_duplicate;

  -- Sync: rows since the cutoff (or written by this function) that left the target or changed,
  -- compared on every column except id and loaded_at, are deleted, then re-inserted below.
  delete from data_staging.stg_timer_activities_clean c
  where (c.start_time >= c_cutoff or c.run_id = c_run_id)
    and not exists (
      select 1 from pg_temp.tmp_timer_clean t
      where t.id = c.id
        and (t.project, t.project_number, t.project_did, t.site_name, t.site_id, t.task, t.site_lat, t.site_long, t.user_lat, t.user_long, t.user_accuracy_m, t.site_vs_user_km, t.start_time, t.end_time, t.duration_min, t.user_name, t.user_email, t.user_role, t.run_id, t.run_date, t.start_date, t.end_date, t.task_clean, t.asset_did) is not distinct from (c.project, c.project_number, c.project_did, c.site_name, c.site_id, c.task, c.site_lat, c.site_long, c.user_lat, c.user_long, c.user_accuracy_m, c.site_vs_user_km, c.start_time, c.end_time, c.duration_min, c.user_name, c.user_email, c.user_role, c.run_id, c.run_date, c.start_date, c.end_date, c.task_clean, c.asset_did)
    );
  get diagnostics v_deleted = row_count;

  insert into data_staging.stg_timer_activities_clean
  select t.* from pg_temp.tmp_timer_clean t
  where not exists (select 1 from data_staging.stg_timer_activities_clean c where c.id = t.id);
  get diagnostics v_inserted = row_count;

  raise notice 'rebuild_timer_clean: % rows in target, % deleted, % inserted', v_src, v_deleted, v_inserted;
end;
$function$;

revoke all on function data_staging.rebuild_timer_clean() from public, anon, authenticated;

comment on function data_staging.rebuild_timer_clean() is
  'Since migration 288 (2026-11-04): refills stg_timer_activities_clean from My Timers (data_staging.stg_timer_entries_clean) for entries since 2025-09-01 PHT; older rows are frozen. Hourly via cron rebuild-timer-clean-hourly.';

select cron.schedule(
  'rebuild-timer-clean-hourly',
  '35 * * * *',
  $cmd$select data_staging.rebuild_timer_clean()$cmd$
);

update agent.schema_metadata
set description = 'The timer entries every report reads (one row per entry)',
    business_context = 'Since migration 288 (2026-11-04) refilled every hour by data_staging.rebuild_timer_clean() from My Timers (data_staging.stg_timer_entries_clean): members'' fixes, entries over 10 hours count as 10 (the end is moved too), exact duplicates counted once, running entries have no end and 0 minutes. Rows before 2025-09-01 PHT are frozen as the old email-form system left them. Read by DRMC hours, revenue, task mix and the gap report.',
    related_tables = array['data_staging.stg_timer_entries_clean', 'data_staging.stg_timer_activities_inc', 'app_my_timers.legacy_entry'],
    updated_at = now()
where schema_name = 'data_staging' and table_name = 'stg_timer_activities_clean' and column_name is null;

update agent.schema_metadata
set description = 'First day of the entry''s ET month',
    business_context = 'For rows since 2025-09-01 PHT: the first day of the ET month the entry started in. Older rows: the first day of the extraction month. Use start_time for the work date.',
    updated_at = now()
where schema_name = 'data_staging' and table_name = 'stg_timer_activities_clean' and column_name = 'start_date';

notify pgrst, 'reload schema';
