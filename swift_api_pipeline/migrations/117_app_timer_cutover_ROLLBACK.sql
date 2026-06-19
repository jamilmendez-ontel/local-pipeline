-- ═══════════════════════════════════════════════════════════════════════════════
-- 117_app_timer_cutover_ROLLBACK.sql
-- Reverses 117: moves the 5 tables back to data_staging, restores the stg_timer_
-- prefix, repoints rebuild_timer_clean() and the agent.schema_metadata rows.
-- NOTE: code (config.py SCHEMA_TIMER, timer_correction_review.py, scripts-reference)
-- must be reverted in lockstep if this rollback is applied.
-- ═══════════════════════════════════════════════════════════════════════════════

BEGIN;

-- 1. Restore prefixed names.
ALTER TABLE app_timer.corrections        RENAME TO stg_timer_corrections;
ALTER TABLE app_timer.entry_removals     RENAME TO stg_timer_entry_removals;
ALTER TABLE app_timer.entry_additions    RENAME TO stg_timer_entry_additions;
ALTER TABLE app_timer.duplicate_reviews  RENAME TO stg_timer_duplicate_reviews;
ALTER TABLE app_timer.daily_notifications RENAME TO stg_timer_daily_notifications;

-- 2. Move back to data_staging.
ALTER TABLE app_timer.stg_timer_corrections         SET SCHEMA data_staging;
ALTER TABLE app_timer.stg_timer_entry_removals      SET SCHEMA data_staging;
ALTER TABLE app_timer.stg_timer_entry_additions     SET SCHEMA data_staging;
ALTER TABLE app_timer.stg_timer_duplicate_reviews   SET SCHEMA data_staging;
ALTER TABLE app_timer.stg_timer_daily_notifications SET SCHEMA data_staging;

-- 3. Restore rebuild_timer_clean() with data_staging references.
--    (Re-apply the function body from migration 114's canonical version.)
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
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
            FROM data_staging.stg_timer_duplicate_reviews r,
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
            FROM data_staging.stg_timer_entry_removals rm
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
                  FROM data_staging.stg_timer_corrections c
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
    FROM data_staging.stg_timer_corrections c
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
    FROM data_staging.stg_timer_entry_additions a
    WHERE NOT EXISTS (
        SELECT 1 FROM data_staging.stg_timer_entry_removals rm
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
        user_email,
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
        corr.user_email,
        '00000000-0000-0000-0000-000000000002'::uuid AS run_id,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS run_date,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS start_date,
        (COALESCE(corr.corrected_end_time, corr.start_time)
            AT TIME ZONE 'America/New_York')::date AS end_date,
        NOW() AS loaded_at
    FROM data_staging.stg_timer_corrections corr
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
      AND NOT EXISTS (
          SELECT 1 FROM data_staging.stg_timer_entry_removals rm
          WHERE rm.project_did = corr.project_did
            AND rm.user_email  = corr.user_email
            AND rm.start_time  = corr.start_time
            AND rm.site_name IS NOT DISTINCT FROM corr.site_name
            AND rm.site_id   IS NOT DISTINCT FROM corr.site_id
            AND rm.task      IS NOT DISTINCT FROM corr.task
            AND rm.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND rm.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
            AND rm.reason IS DISTINCT FROM 'REVERTED'
      );
END;
$function$;

-- 4. Repoint catalog rows back.
UPDATE agent.schema_metadata
   SET schema_name = 'data_staging',
       table_name  = 'stg_timer_' || table_name,
       updated_at  = NOW()
 WHERE schema_name = 'app_timer'
   AND table_name IN ('corrections', 'entry_removals', 'entry_additions',
                      'duplicate_reviews', 'daily_notifications');

-- 5. Drop the (now empty) schema.
DROP SCHEMA IF EXISTS app_timer;

COMMIT;
