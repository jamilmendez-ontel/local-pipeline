-- =============================================================================
-- 292_sheet_feed_ts_skip_when_unchanged.sql
-- OntelDB disk IO: the hourly TS sheet feed rebuild (pg_cron job 41, :55) skips when
-- data_staging.stg_asset_tasks has not moved since the last build.
--
-- STATUS: NOT APPLIED. Safe any time (CREATE OR REPLACE keeps the owner and the
--   postgres/service_role EXECUTE grants; the cron job and its argument are unchanged).
--   This function was never in a migration before; the body below is the live definition
--   from pg_get_functiondef on 2026-11-06 with only the guard block added.
--
-- WHY (pg_stat_statements 2026-11-04 10:02 UTC -> 11-06 ~12:00 UTC)
--   * 49 calls, 11.1M blocks read (~1.8 GB a call), 0.9M temp blocks written (~150 MB a
--     call): every hour it re-reads stg_asset_tasks (3.9 GB) for 8 TS projects and
--     rewrites every CSV page of reference.sheet_feed_pages.
--   * Its only source, stg_asset_tasks, is written by v1's asset-tasks loads (~00:19 and
--     ~05:05 ET full, ~02:36 ET targeted): about 3 changes a day, 24 rebuilds a day.
--
-- THE GUARD
--   max(stg_asset_tasks.loaded_at) rides idx_stg_asset_tasks_loaded_at (one index page).
--   Skip when that is more than 90 minutes older than the last build, and the last build
--   is under 24 h old. The 90 minutes cover a load whose transaction started before the
--   build and committed after it (its rows carry the load's earlier now()): each load
--   still triggers one or two rebuilds, never zero. The 24 h floor rebuilds daily anyway.
--   A skipped call changes nothing and returns {"skipped": true, ...}.
--
-- ROLLBACK
--   Re-run this file with the guard block (between "-- guard" and "-- end guard") removed.
--
-- VERIFY AFTER
--   select public.fn_sheet_feed_build_ts_asset_tasks(10000);   -- {"skipped": true, ...} when
--                                                              -- nothing loaded since the last build
--   select jobid, start_time, status, return_message from cron.job_run_details
--    where jobid = 41 order by start_time desc limit 5;       -- runs of ~0 s between loads
-- =============================================================================

CREATE OR REPLACE FUNCTION public.fn_sheet_feed_build_ts_asset_tasks(p_page_size integer DEFAULT 10000)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'data_staging', 'reference'
AS $function$
declare
  v_header text := '"project_did","project_status","asset_did","task_did","asset_id","asset_name",'
                || '"task_name","task_name_clean","task_status","task_scheduled",'
                || '"task_assigned_to_name","task_submitted_on","task_submitted_by_name",'
                || '"task_approved_on","task_approved_by_name","task_cancelled_on","task_cancelled_by_name",'
                || '"run_id","loaded_at"';
  v_ts     timestamptz := now();
  v_proj   record;
  v_feed   text;
  v_out    jsonb := '[]'::jsonb;
  v_pages  int;
  v_rows   int;
  v_last_build  timestamptz;
  v_last_load   timestamptz;
begin
  -- guard (migration 292): nothing to rebuild when the source has not moved.
  select max(generated_at) into v_last_build
  from reference.sheet_feed_pages where feed ~ '^ts[0-9]+_asset_tasks$';
  select max(loaded_at) into v_last_load from data_staging.stg_asset_tasks;
  if v_last_build is not null and v_last_load is not null
     and v_last_load < v_last_build - interval '90 minutes'
     and v_last_build > v_ts - interval '24 hours' then
    return jsonb_build_object('skipped', true, 'last_build', v_last_build,
                              'last_load', v_last_load);
  end if;
  -- end guard

  for v_proj in
    select p.project_did, p.project_name,
           'ts' || regexp_replace(p.project_name, '^TECH-OPS: TS', '') || '_asset_tasks' as feed
    from data_staging.stg_projects p
    where p.project_name ~ '^TECH-OPS: TS[0-9]+$'
      and exists (select 1 from data_staging.stg_asset_tasks t
                  where t.project_did = p.project_did and t.asset_id ~ '(^|/)AT&T(/|$)')
    order by p.project_name
  loop
    v_feed := v_proj.feed;

    insert into reference.sheet_feed_keys (feed, key_hash, description)
    select v_feed, key_hash, 'Google Sheet "TS20" workbook tab: ' || v_proj.project_name || ', AT&T sites only. Same key as ts20_asset_tasks.'
    from reference.sheet_feed_keys where feed = 'ts20_asset_tasks'
    on conflict (feed) do nothing;

    create temp table _sheet_feed_rows on commit drop as
      select row_number() over (order by p.asset_id, p.task_name, p.task_did) as rn,
             (select string_agg('"' || replace(coalesce(kv.value, ''), '"', '""') || '"', ',' order by kv.ord)
              from json_each_text(row_to_json(p)) with ordinality as kv(key, value, ord)) as line
      from (
        select t.project_did, t.project_status, t.asset_did, t.task_did, t.asset_id, t.asset_name,
               t.task_name, t.task_name_clean, t.task_status, t.task_scheduled,
               t.task_assigned_to_name, t.task_submitted_on, t.task_submitted_by_name,
               t.task_approved_on, t.task_approved_by_name, t.task_cancelled_on, t.task_cancelled_by_name,
               t.run_id, t.loaded_at
        from data_staging.stg_asset_tasks t
        where t.project_did = v_proj.project_did
          and t.asset_id ~ '(^|/)AT&T(/|$)'
      ) p;

    delete from reference.sheet_feed_pages where feed = v_feed;

    insert into reference.sheet_feed_pages (feed, page_no, csv, row_count, generated_at)
    select v_feed,
           (((rn - 1) / p_page_size) + 1)::int,
           v_header || E'\n' || string_agg(line, E'\n' order by rn),
           count(*)::int,
           v_ts
    from _sheet_feed_rows
    group by ((rn - 1) / p_page_size);

    drop table _sheet_feed_rows;

    select count(*), coalesce(sum(row_count), 0) into v_pages, v_rows
    from reference.sheet_feed_pages where feed = v_feed;

    v_out := v_out || jsonb_build_object('feed', v_feed, 'project', v_proj.project_name, 'pages', v_pages, 'rows', v_rows);
  end loop;

  return jsonb_build_object('generated_at', v_ts, 'feeds', v_out);
end
$function$;
