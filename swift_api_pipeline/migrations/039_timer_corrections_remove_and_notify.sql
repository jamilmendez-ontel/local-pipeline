-- Migration 039: Timer corrections — add Remove action + daily email tracking
--
-- 1. Expand stg_timer_corrections.status to allow 'removed'
-- 2. Make corrected_duration_min and corrected_end_time nullable (not needed for removals)
-- 3. Update rebuild_timer_clean() to DELETE removed entries before applying corrections
-- 4. Add stg_timer_daily_notifications table for email thread tracking (reminders)

-- =========================================================================
-- 1. Expand status CHECK to allow 'removed'
-- =========================================================================
ALTER TABLE data_staging.stg_timer_corrections
    DROP CONSTRAINT IF EXISTS stg_timer_corrections_status_check;

ALTER TABLE data_staging.stg_timer_corrections
    ADD CONSTRAINT stg_timer_corrections_status_check
    CHECK (status IN ('corrected', 'removed'));

-- Make duration/end_time nullable for removals
ALTER TABLE data_staging.stg_timer_corrections
    ALTER COLUMN corrected_duration_min DROP NOT NULL,
    ALTER COLUMN corrected_end_time DROP NOT NULL;

-- =========================================================================
-- 2. Daily notification tracking (for reminder threading)
-- =========================================================================
CREATE TABLE IF NOT EXISTS data_staging.stg_timer_daily_notifications (
    id              BIGSERIAL PRIMARY KEY,
    user_email      TEXT NOT NULL,
    send_date       DATE NOT NULL,
    thread_id       TEXT,          -- Gmail thread ID
    message_id      TEXT,          -- RFC Message-ID header
    created_at      TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_email, send_date)
);

-- =========================================================================
-- 3. Update rebuild_timer_clean() — DELETE removed, then UPDATE corrected
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    -- Step 1: Truncate clean table
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 2: Insert from staging, excluding rejected duplicates AND removed entries
    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        -- Exclude rows matching rejected natural keys from resolved reviews
        NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = r.start_time
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        -- For unresolved duplicates, keep only the entry with the latest end_time
        AND NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = r.start_time
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
        -- Exclude entries marked as removed
        AND NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_corrections c
            WHERE c.status = 'removed'
              AND t.project_did = c.project_did
              AND t.user_email  = c.user_email
              AND t.start_time  = c.start_time
              AND t.site_name IS NOT DISTINCT FROM c.site_name
              AND t.site_id   IS NOT DISTINCT FROM c.site_id
              AND t.task      IS NOT DISTINCT FROM c.task
              AND t.end_time IS NOT DISTINCT FROM c.end_time
              AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min
        );

    -- Step 3: Apply duration corrections
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
END;
$$;

-- =========================================================================
-- 4. Schema metadata
-- =========================================================================
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
VALUES
    ('data_staging', 'stg_timer_daily_notifications', 'user_email',
     'Tech email address — one notification row per user per day.'),
    ('data_staging', 'stg_timer_daily_notifications', 'thread_id',
     'Gmail thread ID of the daily entries email. Used for reply-threading reminders.'),
    ('data_staging', 'stg_timer_daily_notifications', 'message_id',
     'RFC Message-ID header of the daily entries email. Used for In-Reply-To threading.')
ON CONFLICT DO NOTHING;

-- Update status description
UPDATE agent.schema_metadata
SET description = 'corrected = duration changed, removed = entry excluded from clean table. Both applied by rebuild_timer_clean() RPC.'
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_timer_corrections'
  AND column_name = 'status';
