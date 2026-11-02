-- =============================================================================
-- 286_stg_timer_entries_clean_duplicates.sql
-- data_staging.stg_timer_entries_clean counts an exact duplicate Swift entry once: a new
-- column is_duplicate, the refresh marks every Swift copy but one (counted_minutes 0), and the
-- parity check gets the cause 'duplicate'. Decision: Jamil 2026-11-02, "counting once".
-- Builds on 285 (applied 2026-11-02); 285's file is never edited.
--
-- STATUS: NOT APPLIED
--
-- WHY
--   * The first parity snapshot after 285 (Sep 22 to Oct 31) had 205 unexplained member-days
--     (+959 h in the new table against v1). 203 of them hold exact duplicate Swift entries:
--     the same owner, start_time, project_did, task_did and asset_did under two or more
--     swift_timer_ids. v1 (stg_timer_activities_clean) counts such a duplicate once; My Timers
--     keeps every copy until the member removes one (the system never removes a timer entry on
--     its own) and the app keeps showing both. Only the warehouse count changes here.
--   * The rule (refresh, after the temp table is filled and its one-row-per-id index built,
--     before the guards and any write): group by (coalesce(emp_id, 'email:' || lower(user_email)),
--     start_time, project_did, task_did, asset_did) where source = 'swift' and project_did is
--     not null and (emp_id or user_email is not null); a closed copy counts before a running one,
--     then the copy with the most actual_minutes (tie: the lowest swift_timer_id in text order,
--     which is fine: any fixed order picks one copy the same way every run); every other copy
--     gets is_duplicate = true and counted_minutes = 0. is_capped stays as computed from
--     actual_minutes. A row with no project is never grouped (both parity sides drop it
--     anyway); nor is a row with neither emp_id nor email (such rows would all share one
--     partition).
--   * Only source = 'swift' is grouped (coordinator ruling, 2026-11-02). A legacy row is a copy
--     of v1's own clean row, so a legacy pair is one v1 itself counts twice; marking it would
--     make new differences from v1 (production: 121 legacy copies in 118 groups, against 1,053
--     Swift copies in 900 groups). A request row (an approved missing entry) is never grouped.
--     A legacy row and a Swift row with the same start are never grouped either. A null task_did or asset_did groups with the same null (window partition).
--     Duplicates share start_time, so every copy is always in the same refresh window.
--     Closed first: a running copy starts with the closed one, so its minutes pass the closed
--     one's as soon as that ended; v1 counts a running timer as 0 and so counts the closed
--     copy. Ordering by (end_time is null) first keeps the closed copy counted, which matches
--     v1 and never flips while the running copy grows. Of two running copies (or two closed
--     ones) the longest counts.
--   * The 285 body is kept as is; the only changes to the refresh are the mark and the new
--     column in the insert list, the `is not distinct from` compare tuple and the on-conflict
--     update (a column missing from the tuple would never update: the asset_id drift class).
--     Same signature, settings (search_path '', statement_timeout 300s, work_mem 64MB) and
--     revokes. The temp table is `like ... including defaults`, so it gets the column.
--   * The parity: duplicates now count 0, so such a day should match v1. Two changes:
--     the cap check leaves the duplicates out of its uncapped sum and of its capped flag (a
--     capped duplicate copy would otherwise make a day read as cap, or a capped day with a
--     duplicate never read as cap), and a new cause 'duplicate' for a day with an
--     is_duplicate row whose difference remains (for example v1 kept the shorter copy).
--     Cause order, the first that applies: cap, running, my_timers_fix, unowned_in_my_timers,
--     v1_fix_not_carried, only_in_my_timers, only_in_v1, duplicate, unexplained.
--   * Writes: the column has a constant default (no table rewrite; a catalog change only).
--     The first run after apply rewrites each duplicate copy once (counted 0, is_duplicate
--     true); after that nothing changes unless a copy changes. lock_timeout 10s keeps the
--     alter from queueing behind a running refresh for long (rerun the file if it times out).
--   * The pg_cron jobs of 285 call the same function names with the same signatures: no job
--     changes.
--
-- PREFLIGHT (the DO block below refuses to apply on any of these; read-only):
--   select to_regclass('data_staging.stg_timer_entries_clean');             -- not null
--   select count(*) from information_schema.columns where table_schema = 'data_staging'
--     and table_name = 'stg_timer_entries_clean' and column_name = 'is_duplicate';  -- 0
--   select to_regprocedure('data_staging.refresh_timer_entries_clean(timestamptz,timestamptz)'),
--          to_regprocedure('data_staging.timer_entries_parity(date,date)');  -- both not null
--   select count(*) from agent.schema_metadata where schema_name = 'data_staging'
--     and table_name = 'stg_timer_entries_clean' and column_name is null;   -- 1
--   To size the first run (copies that will count 0):
--   select count(*) as groups, sum(n - 1) as copies from (
--     select count(*) as n from data_staging.stg_timer_entries_clean
--     where project_did is not null and source = 'swift'
--     group by coalesce(emp_id, 'email:' || lower(user_email)), start_time, project_did, task_did, asset_did
--     having count(*) > 1) g;
--
-- ROLLBACK (in this order, in one transaction: begin; set local lock_timeout = '10s'; ... commit;
--   the drop column takes an access exclusive lock; the timeout keeps it from queueing long
--   behind a running cron refresh):
--   1. Re-create both functions from 285: run the two `create function` statements of
--      285_stg_timer_entries_clean.sql (section 3, the refresh, and section 4, the parity) as
--      `create or replace function`, with their revoke lines. Their bodies do not name
--      is_duplicate, so this goes first.
--   2. alter table data_staging.stg_timer_entries_clean drop column if exists is_duplicate;
--      (the next refresh rewrites the duplicate copies back to their own counted minutes)
--   3. delete from agent.schema_metadata where schema_name = 'data_staging'
--        and table_name = 'stg_timer_entries_clean' and column_name = 'is_duplicate';
--   4. Put back the 285 texts of the four updated metadata rows (stg_timer_entries_clean: the
--      table row data_notes and counted_minutes description; stg_timer_entries_parity: the
--      table row data_notes and cause description): the values in section 7 of 285.
--
-- VERIFY (after apply):
--   select count(*) filter (where is_duplicate) from data_staging.stg_timer_entries_clean;  -- 0 before a run
--   select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day');
--     -- {"inserted": 0, "deleted": 0, "updated": about 1,053 on production (the Swift copies of
--     --  the PREFLIGHT sizing, 900 groups on 2026-11-02) plus the running entries}
--   select data_staging.refresh_timer_entries_clean(timestamptz '2025-08-31 16:00:00+00', now() + interval '1 day');
--     -- {"inserted": 0, "deleted": 0, "updated": <running entries>}
--   select count(*), sum(actual_minutes) / 60 as hours_not_counted
--     from data_staging.stg_timer_entries_clean where is_duplicate;
--   select count(*) from data_staging.stg_timer_entries_clean
--     where is_duplicate and counted_minutes <> 0;                       -- 0
--   select source, count(*) from data_staging.stg_timer_entries_clean
--     where is_duplicate group by 1;                                     -- swift only
--   select data_staging.snapshot_timer_entries_parity();
--   select cause, count(*), sum(new_minutes - old_minutes) from data_staging.stg_timer_entries_parity
--     group by 1 order by 2 desc;
--     -- 'unexplained' drops from 205 to a handful; 'duplicate' lists days where v1 kept another copy
--   select count(*) from agent.schema_metadata where schema_name = 'data_staging'
--     and table_name = 'stg_timer_entries_clean' and column_name = 'is_duplicate';  -- 1
-- =============================================================================

begin;

set local lock_timeout = '10s';

-- 0. PREFLIGHT gates.
do $$
begin
  if to_regclass('data_staging.stg_timer_entries_clean') is null then
    raise exception '286: data_staging.stg_timer_entries_clean does not exist. Apply 285 first.';
  end if;
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'data_staging' and table_name = 'stg_timer_entries_clean'
      and column_name = 'is_duplicate'
  ) then
    raise exception '286: stg_timer_entries_clean.is_duplicate exists already. Stop and look.';
  end if;
  if to_regprocedure('data_staging.refresh_timer_entries_clean(timestamptz,timestamptz)') is null
     or to_regprocedure('data_staging.timer_entries_parity(date,date)') is null then
    raise exception '286: a 285 function is missing (refresh_timer_entries_clean or timer_entries_parity).';
  end if;
  if not exists (
    select 1 from agent.schema_metadata
    where schema_name = 'data_staging' and table_name = 'stg_timer_entries_clean' and column_name is null
  ) then
    raise exception '286: the table row of stg_timer_entries_clean in agent.schema_metadata is missing (285 section 7).';
  end if;
end $$;

-- 1. The column. A constant default: a catalog change, no table rewrite.
alter table data_staging.stg_timer_entries_clean
  add column is_duplicate boolean not null default false;

-- 2. The refresh: the 285 body plus the duplicate mark (see WHY).
create or replace function data_staging.refresh_timer_entries_clean(
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

  -- 286: an exact duplicate Swift entry counts once (Jamil, 2026-11-02). Same owner, start,
  -- project, task and asset: a closed copy counts before a running one (v1 counts a running
  -- timer as 0, so v1 counts the closed copy), then the copy with the most actual minutes
  -- (tie: the lowest swift_timer_id); every other copy keeps its row, is_duplicate true,
  -- counted 0. is_capped stays as computed from actual_minutes. Only Swift rows are grouped: a
  -- legacy row is a copy of v1's own clean row (v1 counts a legacy pair twice) and a request
  -- row is never a duplicate. A row with no project, or with neither owner nor email, is never
  -- grouped. My Timers keeps showing every copy until the member removes one.
  update pg_temp._tec t
     set is_duplicate = true,
         counted_minutes = 0
    from (
      select x.swift_timer_id,
             row_number() over (
               partition by coalesce(x.emp_id, 'email:' || lower(x.user_email)),
                            x.start_time, x.project_did, x.task_did, x.asset_did
               order by (x.end_time is null), x.actual_minutes desc, x.swift_timer_id) as rn
      from pg_temp._tec x
      where x.project_did is not null and x.source = 'swift'
        and (x.emp_id is not null or x.user_email is not null)
    ) d
   where d.swift_timer_id = t.swift_timer_id
     and d.rn > 1;
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
      refreshed_at, is_duplicate
    )
    select t.swift_timer_id, t.emp_id, t.user_email, t.project_did, t.project_number, t.asset_did,
           t.site_name, t.task_did, t.task_clean, t.task_name, t.start_time, t.end_time,
           t.work_date, t.actual_minutes, t.counted_minutes, t.is_running, t.is_capped, t.source,
           t.edited_fields, t.fixed_by_super_admin, t.refreshed_at, t.is_duplicate
    from pg_temp._tec t
    where not exists (
      select 1 from data_staging.stg_timer_entries_clean c
      where c.swift_timer_id = t.swift_timer_id
        and (c.emp_id, c.user_email, c.project_did, c.project_number, c.asset_did, c.site_name, c.task_did, c.task_clean, c.task_name, c.start_time, c.end_time, c.work_date, c.actual_minutes, c.counted_minutes, c.is_running, c.is_capped, c.source, c.edited_fields, c.fixed_by_super_admin, c.is_duplicate) is not distinct from
            (t.emp_id, t.user_email, t.project_did, t.project_number, t.asset_did, t.site_name, t.task_did, t.task_clean, t.task_name, t.start_time, t.end_time, t.work_date, t.actual_minutes, t.counted_minutes, t.is_running, t.is_capped, t.source, t.edited_fields, t.fixed_by_super_admin, t.is_duplicate)
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
      is_duplicate = excluded.is_duplicate,
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
-- 3. The parity check: the 285 body plus the cause 'duplicate' and the uncapped sum without
--    the duplicates. Cause, first that applies: cap, running, my_timers_fix,
--    unowned_in_my_timers, v1_fix_not_carried, only_in_my_timers, only_in_v1, duplicate (the
--    day has an is_duplicate row and still differs, e.g. v1 kept another copy), else
--    unexplained.
create or replace function data_staging.timer_entries_parity(p_from date, p_to date)
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
           n.is_duplicate,
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
           sum(r.actual_minutes) filter (where not r.is_duplicate) as uncapped, bool_or(r.is_capped) filter (where not r.is_duplicate) as capped,
           coalesce(sum(r.counted_minutes) filter (where not r.is_running), 0) as closed,
           bool_or(r.is_running) as running,
           bool_or(r.fixed) as fixed, bool_or(r.unowned_emp is not null) as unowned,
           bool_or(r.is_duplicate) as duplicated
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
           coalesce(n.unowned, false) as unowned,
           coalesce(n.duplicated, false) as duplicated
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
           when d.duplicated then 'duplicate'
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
-- 4. The record for the AI semantic layer: the new column, and the texts that name the rule.
insert into agent.schema_metadata
  (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
select v.schema_name, v.table_name, v.column_name, v.description, v.business_context, v.data_notes, v.related_tables
from (values
  ('data_staging', 'stg_timer_entries_clean', 'is_duplicate',
   'True for an exact duplicate copy of a Swift entry (source swift; same owner, start_time, project_did, task_did and asset_did as another Swift row, another swift_timer_id) that does not count: its counted_minutes is 0.',
   'An exact duplicate counts once (Jamil, 2026-11-02): the copy with the most actual_minutes counts (tie: the lowest swift_timer_id). Summing counted_minutes adds nothing for a duplicate; filter not is_duplicate to count entries.',
   'Set by data_staging.refresh_timer_entries_clean (migration 286). Only source swift rows are marked: a legacy row is a copy of v1''s own clean row (v1 counts a legacy pair twice) and a request row is never a duplicate. Rows with no project_did are never marked. My Timers still shows every copy until the member removes one; actual_minutes and is_capped keep the copy''s own clock time.',
   array['data_staging.stg_timer_activities_clean']::text[])
) as v(schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
where not exists (
  select 1 from agent.schema_metadata m
  where m.schema_name = v.schema_name and m.table_name = v.table_name
    and m.column_name is not distinct from v.column_name
);

update agent.schema_metadata
set data_notes = 'Owner: My Timers. Grain: one row per timer entry (swift_timer_id). Source: data_staging.refresh_timer_entries_clean() from app_my_timers.entries_layout: hourly (pg_cron timer-entries-clean-hourly, last 40 days) and nightly (timer-entries-clean-nightly, from 2025-09-01 Manila); the jobs are created inactive until ACTIVATE (migration 285 header). Cap: counted_minutes = least(actual_minutes, 600); a running entry counts up to the refresh time. Duplicates counted once (migration 286): of an exact duplicate Swift entry (source swift only; same owner, start_time, project_did, task_did, asset_did) only the copy with the most actual_minutes counts; every other copy keeps its row with is_duplicate true and counted_minutes 0. Writes only changed rows. The My Timers app never reads this table. PII: yes, user_email (employee email). Retention: kept as long as data_staging.stg_timer_activities_clean (no purge of its own).'
where schema_name = 'data_staging' and table_name = 'stg_timer_entries_clean'
  and column_name is null;

update agent.schema_metadata
set description = 'least(actual_minutes, 600): the minutes that count; 0 for a duplicate copy (is_duplicate).'
where schema_name = 'data_staging' and table_name = 'stg_timer_entries_clean'
  and column_name = 'counted_minutes';

update agent.schema_metadata
set data_notes = 'Owner: My Timers. Grain: one row per member-day that differs. Source: data_staging.snapshot_timer_entries_parity() (pg_cron timer-entries-clean-parity, 19:55 UTC) after the nightly full refresh, for the 40 Manila days ending two days ago (v1 loads at 13:15 PHT). Key: emp_id, else the lower email (member_key). Causes: cap, running, my_timers_fix, unowned_in_my_timers, v1_fix_not_carried, only_in_my_timers, only_in_v1, duplicate, unexplained. PII: yes, user_email (employee email). Retention: the last snapshot only (replaced every night).'
where schema_name = 'data_staging' and table_name = 'stg_timer_entries_parity'
  and column_name is null;

update agent.schema_metadata
set description = 'Why the day differs, the first that applies: cap, running, my_timers_fix, unowned_in_my_timers, v1_fix_not_carried, only_in_my_timers, only_in_v1, duplicate (a duplicate copy on the day and the day still differs, e.g. v1 kept another copy), unexplained.'
where schema_name = 'data_staging' and table_name = 'stg_timer_entries_parity'
  and column_name = 'cause';

commit;
