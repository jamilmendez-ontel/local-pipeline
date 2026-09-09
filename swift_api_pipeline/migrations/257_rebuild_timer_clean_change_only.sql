-- =============================================================================
-- 257_rebuild_timer_clean_change_only.sql
-- WAL diet item 3 (2026-09-09): rebuild_timer_clean() writes only the
-- difference.
--
-- Before: DELETE all ~400k rows of data_staging.stg_timer_activities_clean
-- (571 MB, 15 indexes) then re-INSERT them, on every call. Measured
-- 2026-09-04..08: 48 calls, 34.3 GB of WAL (~0.66 GB per call, ~6.7 GB/day);
-- 8 of the ~10 daily calls are correction/removal dispatches that change a
-- handful of rows. The 2026-09-04 crash loop was this rebuild running 20-43x
-- a day.
--
-- After: the same steps build the target set into a session TEMP table
-- (unlogged, zero WAL) and a sync deletes clean rows that left the set or
-- changed, then inserts the missing ones. Identity is `id` (stg row id for
-- steps 2/4, app_timer.entry_additions id; synthetic correction rows reuse
-- the clean id by natural key), content compare is every column except id
-- and loaded_at. The nightly timer load replaces the current month's stg
-- rows with new ids, so the two nightly rebuilds still rewrite that month
-- (~12k rows, ~30 MB); the correction/removal rebuilds write almost nothing.
--
-- Readers: still no TRUNCATE (218's reason stands: RowExclusiveLock, readers
-- see the pre-sync snapshot). loaded_at on clean keeps meaning what it meant
-- (copied from stg / NOW() for synthetic rows); MAX(loaded_at) freshness
-- probes are unaffected.
--
-- Body provenance: the live function (pg_get_functiondef md5
-- 41b4ed15853418b5496ac7e2ece61ff0, 11,050 chars, == migration 241 text)
-- with `data_staging.stg_timer_activities_clean` renamed to the temp table
-- in all seven write/self-reference sites and the leading DELETE removed.
-- The preflight below refuses to apply on any drift.
-- =============================================================================

DO $$
DECLARE def text;
BEGIN
  def := pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure);
  IF md5(def) <> '41b4ed15853418b5496ac7e2ece61ff0' THEN
    IF position('tmp_timer_clean' IN def) > 0 THEN
      RAISE EXCEPTION '257: already applied (live body is the change-only version)';
    END IF;
    RAISE EXCEPTION '257: live rebuild_timer_clean drifted from the captured 241 body (md5 %); re-capture before applying', md5(def);
  END IF;
END $$;

CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
DECLARE
    v_deleted  bigint;
    v_inserted bigint;
BEGIN
    -- 257 (WAL diet): build the WHOLE target set in a session temp table
    -- (unlogged: no WAL), then write only the difference into clean. Before
    -- this the function deleted and re-inserted all ~400k rows (571 MB, 15
    -- indexes) on every call, ~0.66 GB of WAL x ~10 calls/day (8 of them
    -- correction/removal dispatches that change a handful of rows).
    -- Every filter/step below is the 241 body verbatim with the write target
    -- renamed; the sync at the end is the only new logic.
    CREATE TEMP TABLE IF NOT EXISTS tmp_timer_clean
        (LIKE data_staging.stg_timer_activities_clean INCLUDING DEFAULTS) ON COMMIT DROP;
    TRUNCATE tmp_timer_clean;

    INSERT INTO tmp_timer_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = COALESCE((rej->>'start_time')::timestamptz, r.start_time)
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (e->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.entry_removals rm
            WHERE t.project_did = rm.project_did
              AND t.user_email  = rm.user_email
              AND t.start_time  = rm.start_time
              AND t.site_name IS NOT DISTINCT FROM rm.site_name
              AND t.site_id   IS NOT DISTINCT FROM rm.site_id
              AND t.task      IS NOT DISTINCT FROM rm.task
              AND t.end_time IS NOT DISTINCT FROM rm.end_time
              AND t.duration_min IS NOT DISTINCT FROM rm.duration_min
              AND rm.reason IS DISTINCT FROM 'REVERTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_timer.corrections c
                  WHERE c.project_did = rm.project_did
                    AND c.user_email  = rm.user_email
                    AND c.start_time  = rm.start_time
                    AND c.site_name IS NOT DISTINCT FROM rm.site_name
                    AND c.site_id   IS NOT DISTINCT FROM rm.site_id
                    AND c.task      IS NOT DISTINCT FROM rm.task
                    AND c.end_time IS NOT DISTINCT FROM rm.end_time
                    AND c.original_duration_min IS NOT DISTINCT FROM rm.duration_min
              )
        )
    ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
             t.task, t.end_time, t.duration_min, t.id;

    UPDATE tmp_timer_clean t
    SET duration_min = c.corrected_duration_min,
        end_time    = c.corrected_end_time
    FROM app_timer.corrections c
    WHERE c.status = 'corrected'
      AND t.project_did = c.project_did
      AND t.user_email  = c.user_email
      AND t.start_time  = c.start_time
      AND t.site_name IS NOT DISTINCT FROM c.site_name
      AND t.site_id   IS NOT DISTINCT FROM c.site_id
      AND t.task      IS NOT DISTINCT FROM c.task
      AND t.end_time IS NOT DISTINCT FROM c.end_time
      AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min;

    INSERT INTO tmp_timer_clean (
        id, project, project_number, project_did, site_name, site_id,
        task, task_clean, site_lat, site_long, user_lat, user_long,
        user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT
        a.id, a.project, a.project_number, a.project_did, a.site_name, a.site_id,
        a.task, a.task_clean, a.site_lat, a.site_long, a.user_lat, a.user_long,
        a.user_accuracy_m, a.site_vs_user_km, a.start_time, a.end_time, a.duration_min,
        a.user_name, a.user_email, a.user_role,
        a.run_id, a.run_date,
        COALESCE(a.start_date, (a.start_time AT TIME ZONE 'America/New_York')::date),
        COALESCE(a.end_date,   (a.start_time AT TIME ZONE 'America/New_York')::date),
        a.loaded_at
    FROM app_timer.entry_additions a
    WHERE NOT EXISTS (
        SELECT 1 FROM app_timer.entry_removals rm
        WHERE rm.project_did = a.project_did
          AND rm.user_email  = a.user_email
          AND rm.start_time  = a.start_time
          AND rm.site_name IS NOT DISTINCT FROM a.site_name
          AND rm.site_id   IS NOT DISTINCT FROM a.site_id
          AND rm.task      IS NOT DISTINCT FROM a.task
          AND rm.end_time IS NOT DISTINCT FROM a.end_time
          AND rm.duration_min IS NOT DISTINCT FROM a.duration_min
          AND rm.reason IS DISTINCT FROM 'REVERTED'
    );

    INSERT INTO tmp_timer_clean (
        project, project_number, project_did, site_name, site_id,
        task, task_clean, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT DISTINCT ON (corr.project_did, corr.user_email, corr.start_time,
                        corr.site_name, corr.site_id, corr.task)
        corr.project,
        NULL::integer AS project_number,
        corr.project_did, corr.site_name, corr.site_id,
        corr.task,
        regexp_replace(corr.task, '^\d+\.\s+', '') AS task_clean,
        corr.start_time, corr.corrected_end_time, corr.corrected_duration_min,
        nm.user_name, corr.user_email, nm.user_role,
        '00000000-0000-0000-0000-000000000002'::uuid AS run_id,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS run_date,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS start_date,
        (COALESCE(corr.corrected_end_time, corr.start_time)
            AT TIME ZONE 'America/New_York')::date AS end_date,
        NOW() AS loaded_at
    FROM app_timer.corrections corr
    LEFT JOIN (
        SELECT user_email,
               (array_agg(user_name ORDER BY start_time DESC)
                  FILTER (WHERE user_name IS NOT NULL AND user_name <> ''))[1] AS user_name,
               (array_agg(user_role ORDER BY start_time DESC)
                  FILTER (WHERE user_role IS NOT NULL AND user_role <> ''))[1] AS user_role
        FROM data_staging.stg_timer_activities
        GROUP BY user_email
    ) nm ON nm.user_email = corr.user_email
    WHERE corr.status = 'corrected'
      AND NOT EXISTS (
          SELECT 1 FROM tmp_timer_clean t
          WHERE t.project_did = corr.project_did
            AND t.user_email  = corr.user_email
            AND t.start_time  = corr.start_time
            AND t.site_name IS NOT DISTINCT FROM corr.site_name
            AND t.site_id   IS NOT DISTINCT FROM corr.site_id
            AND t.task      IS NOT DISTINCT FROM corr.task
            AND t.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND t.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      )
      -- NOTE: unlike every other removals anti-join, the LIVE function has no
      -- rm.reason REVERTED exclusion here (drift vs committed 197, preserved
      -- deliberately by 218; do not "restore" it without a decision).
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = corr.project_did
            AND rm.user_email  = corr.user_email
            AND rm.start_time  = corr.start_time
            AND rm.site_name IS NOT DISTINCT FROM corr.site_name
            AND rm.site_id   IS NOT DISTINCT FROM corr.site_id
            AND rm.task      IS NOT DISTINCT FROM corr.task
            AND rm.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND rm.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      );

    -- Step 5 (migration 197): drop UNTRACKED same-start runaway duplicates.
    DELETE FROM tmp_timer_clean cln
    WHERE cln.duration_min > 720
      AND EXISTS (
          SELECT 1 FROM tmp_timer_clean t2
          WHERE t2.project_did = cln.project_did
            AND t2.user_email  = cln.user_email
            AND t2.start_time  = cln.start_time
            AND t2.site_name IS NOT DISTINCT FROM cln.site_name
            AND t2.site_id   IS NOT DISTINCT FROM cln.site_id
            AND t2.task      IS NOT DISTINCT FROM cln.task
            AND t2.duration_min <= 720
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.corrections c
          WHERE c.project_did = cln.project_did
            AND c.user_email  = cln.user_email
            AND c.start_time  = cln.start_time
            AND c.site_name IS NOT DISTINCT FROM cln.site_name
            AND c.site_id   IS NOT DISTINCT FROM cln.site_id
            AND c.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = cln.project_did
            AND rm.user_email  = cln.user_email
            AND rm.start_time  = cln.start_time
            AND rm.site_name IS NOT DISTINCT FROM cln.site_name
            AND rm.site_id   IS NOT DISTINCT FROM cln.site_id
            AND rm.task      IS NOT DISTINCT FROM cln.task
            -- 241: a removal captured while the timer was still running
            -- (end_time NULL) is not a member decision about the completed
            -- rows, so it must not shield later runaway duplicates.
            AND rm.end_time IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.duplicate_reviews r
          WHERE r.project_did = cln.project_did
            AND r.user_email  = cln.user_email
            AND r.start_time  = cln.start_time
            AND r.site_name IS NOT DISTINCT FROM cln.site_name
            AND r.site_id   IS NOT DISTINCT FROM cln.site_id
            AND r.task      IS NOT DISTINCT FROM cln.task
      );

    -- ---------------------------------------------------------------------
    -- 257: sync the target set into clean, touching only rows that differ.
    -- ---------------------------------------------------------------------
    -- Correction-created rows (synthetic run_id ...0002) get a fresh serial
    -- id every build; reuse the clean row's id when the natural key matches
    -- so they are not deleted and re-inserted on every rebuild.
    UPDATE tmp_timer_clean t
    SET id = c.id
    FROM data_staging.stg_timer_activities_clean c
    WHERE t.run_id = '00000000-0000-0000-0000-000000000002'::uuid
      AND c.run_id = '00000000-0000-0000-0000-000000000002'::uuid
      AND c.project_did IS NOT DISTINCT FROM t.project_did AND c.user_email IS NOT DISTINCT FROM t.user_email AND c.start_time IS NOT DISTINCT FROM t.start_time AND c.site_name IS NOT DISTINCT FROM t.site_name AND c.site_id IS NOT DISTINCT FROM t.site_id AND c.task IS NOT DISTINCT FROM t.task AND c.end_time IS NOT DISTINCT FROM t.end_time AND c.duration_min IS NOT DISTINCT FROM t.duration_min;

    -- Rows that left the target set, or whose content changed (compared on
    -- every column except id and loaded_at): delete, then re-insert below.
    DELETE FROM data_staging.stg_timer_activities_clean c
    WHERE NOT EXISTS (
        SELECT 1 FROM tmp_timer_clean t
        WHERE t.id = c.id
          AND (t.project, t.project_number, t.project_did, t.site_name, t.site_id, t.task, t.site_lat, t.site_long, t.user_lat, t.user_long, t.user_accuracy_m, t.site_vs_user_km, t.start_time, t.end_time, t.duration_min, t.user_name, t.user_email, t.user_role, t.run_id, t.run_date, t.start_date, t.end_date, t.task_clean, t.asset_did) IS NOT DISTINCT FROM (c.project, c.project_number, c.project_did, c.site_name, c.site_id, c.task, c.site_lat, c.site_long, c.user_lat, c.user_long, c.user_accuracy_m, c.site_vs_user_km, c.start_time, c.end_time, c.duration_min, c.user_name, c.user_email, c.user_role, c.run_id, c.run_date, c.start_date, c.end_date, c.task_clean, c.asset_did)
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT t.* FROM tmp_timer_clean t
    WHERE NOT EXISTS (SELECT 1 FROM data_staging.stg_timer_activities_clean c WHERE c.id = t.id);
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    RAISE NOTICE 'rebuild_timer_clean: % rows in target, % deleted, % inserted',
        (SELECT count(*) FROM tmp_timer_clean), v_deleted, v_inserted;
END;
$function$;

-- ---------------------------------------------------------------------------
-- VERIFY (run once in a quiet window, right after apply):
--   SELECT count(*), md5(string_agg(md5(row(id,project,project_number,project_did,
--     site_name,site_id,task,site_lat,site_long,user_lat,user_long,user_accuracy_m,
--     site_vs_user_km,start_time,end_time,duration_min,user_name,user_email,user_role,
--     run_id,run_date,start_date,end_date,task_clean,asset_did)::text), ',' ORDER BY id))
--   FROM data_staging.stg_timer_activities_clean;      -- before
--   SELECT data_staging.rebuild_timer_clean();          -- NOTICE: n in target, d deleted, i inserted
--   (same SELECT)                                       -- after: identical when inputs did not change
-- ROLLBACK: re-apply migration 241's CREATE OR REPLACE FUNCTION body.
-- ---------------------------------------------------------------------------
