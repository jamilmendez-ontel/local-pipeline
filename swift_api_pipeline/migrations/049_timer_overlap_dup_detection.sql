-- Migration 049: Overlap-based duplicate detection support
--
-- Code change in timer_correction_review.py extends duplicate detection from
-- "same start_time only" to "any temporal overlap on the same task". Within
-- a cluster, entries can now have different start_times. The parent
-- start_time column on stg_timer_duplicate_reviews holds the cluster anchor
-- (earliest start_time); per-entry start_times live in the JSONB entries[]
-- and rejected_entries[] arrays.
--
-- This migration:
--   1. Backfills existing review rows so each JSONB entry carries start_time
--      explicitly. Today every entry in a same-start cluster shares the
--      parent column, so we copy from there. Idempotent guard skips rows
--      already migrated.
--   2. Rewrites data_staging.rebuild_timer_clean() to join on per-entry
--      start_time inside JSONB instead of the parent column.

-- =========================================================================
-- 1. Idempotent JSONB backfill: inject start_time into entries[] and
--    rejected_entries[] where missing.
-- =========================================================================
UPDATE data_staging.stg_timer_duplicate_reviews
SET entries = (
    SELECT jsonb_agg(
        CASE
            WHEN elem ? 'start_time' THEN elem
            ELSE jsonb_set(elem, '{start_time}', to_jsonb((start_time AT TIME ZONE 'UTC')::text || '+00:00'))
        END
        ORDER BY ord
    )
    FROM jsonb_array_elements(entries) WITH ORDINALITY AS t(elem, ord)
)
WHERE entries IS NOT NULL
  AND jsonb_array_length(entries) > 0
  AND NOT (entries->0 ? 'start_time');

UPDATE data_staging.stg_timer_duplicate_reviews
SET rejected_entries = (
    SELECT jsonb_agg(
        CASE
            WHEN elem ? 'start_time' THEN elem
            ELSE jsonb_set(elem, '{start_time}', to_jsonb((start_time AT TIME ZONE 'UTC')::text || '+00:00'))
        END
        ORDER BY ord
    )
    FROM jsonb_array_elements(rejected_entries) WITH ORDINALITY AS t(elem, ord)
)
WHERE rejected_entries IS NOT NULL
  AND jsonb_array_length(rejected_entries) > 0
  AND NOT (rejected_entries->0 ? 'start_time');

-- =========================================================================
-- 2. Replace rebuild_timer_clean() to join on per-entry start_time from JSONB.
--    Differences from migration 048:
--      - Rejected-entry exclusion join uses (rej->>'start_time')::timestamptz
--        instead of r.start_time.
--      - Unresolved "keep latest end_time" subquery uses
--        (e->>'start_time')::timestamptz instead of r.start_time.
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 1: Insert from staging, excluding rejected duplicates + removals.
    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        -- Exclude rows matching rejected natural keys from resolved reviews.
        -- Now joins on per-entry start_time inside JSONB.
        NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (rej->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        -- For unresolved duplicates, keep only the entry with the latest end_time.
        -- Now joins on per-entry start_time inside JSONB.
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
        -- Exclude removed entries UNLESS reverted or overridden by correction.
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

    -- Step 2: Apply duration corrections (unchanged from migration 048).
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

    -- Step 3: Append manual additions (unchanged from migration 048).
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
    FROM data_staging.stg_timer_entry_additions a;
END;
$$;
