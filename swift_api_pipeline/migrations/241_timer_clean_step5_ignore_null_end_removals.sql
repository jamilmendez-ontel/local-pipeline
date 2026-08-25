-- 241: Step 5 of rebuild_timer_clean() must not treat a removal captured
-- against a still-running snapshot (entry_removals.end_time IS NULL) as
-- "tracked". Jamil 2026-08-24.
--
-- Context: the nightly Timer Activity Entries email used to list running
-- timers (end_time NULL, 0 min) with Remove buttons. A Remove there stores a
-- removal keyed to end_time NULL. Every removals anti-join matches on the
-- exact natural key, so the removal is inert once the timer completes; but
-- Step 5 (migration 197: drop UNTRACKED same-start runaway duplicates
-- > 720 min when a <= 720 sibling exists) anti-joins entry_removals on the
-- start-key only, so that inert removal SHIELDS the runaway completions.
-- Row-level preflight 2026-08-24: two prince groups survive in clean for
-- exactly this reason (2026-07-29 22.9h next to 3.5h; 2026-08-19 21.9h x2
-- next to 2.2h), 66.7h total; no duplicate_reviews rows on either start-key.
-- Expected effect of applying: those three rows leave clean, nothing else
-- changes. The email side of the fix (running timers no longer actionable,
-- "timer still running" notice) ships in the same branch:
-- docs/superpowers/specs/2026-08-24-timer-running-entries-design.md
--
-- ONLY the Step 5 entry_removals anti-join changed (one added predicate).
-- Everything else is the live body verbatim (captured via
-- pg_get_functiondef 2026-08-24, identical to migration 218's body; the
-- step-0 preflight aborts on drift).

-- ---------------------------------------------------------------------------
-- 0) Preflight: abort if the live function drifted from the captured body or
--    241 is already applied.
-- ---------------------------------------------------------------------------
DO $$
DECLARE def text;
BEGIN
  def := pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure);
  IF position('AND rm.end_time IS NOT NULL' IN def) > 0 THEN
    RAISE EXCEPTION '241: Step 5 already has the end_time predicate; migration appears applied';
  END IF;
  IF position('DELETE FROM data_staging.stg_timer_activities_clean;' IN def) = 0 THEN
    RAISE EXCEPTION '241: live rebuild_timer_clean is not the 218 body (no DELETE line); re-capture before applying';
  END IF;
  IF position('Step 5 (migration 197)' IN def) = 0
     OR position('AND rm.task      IS NOT DISTINCT FROM cln.task' IN def) = 0 THEN
    RAISE EXCEPTION '241: live rebuild_timer_clean missing the Step 5 removals anti-join; body drifted, re-capture before applying';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
BEGIN
    -- 218: DELETE, not TRUNCATE. RowExclusiveLock lets concurrent readers see
    -- the pre-rebuild snapshot instead of queuing on AccessExclusiveLock.
    DELETE FROM data_staging.stg_timer_activities_clean;

    INSERT INTO data_staging.stg_timer_activities_clean
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

    UPDATE data_staging.stg_timer_activities_clean t
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

    INSERT INTO data_staging.stg_timer_activities_clean (
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

    INSERT INTO data_staging.stg_timer_activities_clean (
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
          SELECT 1 FROM data_staging.stg_timer_activities_clean t
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
    DELETE FROM data_staging.stg_timer_activities_clean cln
    WHERE cln.duration_min > 720
      AND EXISTS (
          SELECT 1 FROM data_staging.stg_timer_activities_clean t2
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
END;
$function$;

-- ---------------------------------------------------------------------------
-- ROLLBACK: re-apply migration 218's CREATE OR REPLACE FUNCTION body (the
-- pre-241 live body is exactly that). No other object changed.
-- ---------------------------------------------------------------------------
