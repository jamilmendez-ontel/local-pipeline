-- Migration 037: Support N entries per duplicate group (not just A/B)
--
-- Replaces fixed entry_a/entry_b columns with JSONB arrays:
--   entries: all entries in the group [{label, end_time, duration_min}, ...]
--   rejected_entries: natural keys of rejected entries [{end_time, duration_min}, ...]
--
-- Adds site_name, site_id, task as group-level columns (part of duplicate key).
-- Duplicate key: (project_did, user_email, start_time, site_name, site_id, task)
--
-- selected_entry now allows any uppercase letter (A-Z).

-- =========================================================================
-- 1. Add new columns
-- =========================================================================
ALTER TABLE data_staging.stg_timer_duplicate_reviews
    ADD COLUMN IF NOT EXISTS entries JSONB,
    ADD COLUMN IF NOT EXISTS rejected_entries JSONB,
    ADD COLUMN IF NOT EXISTS notification_thread_id TEXT,
    ADD COLUMN IF NOT EXISTS notification_message_id TEXT,
    ADD COLUMN IF NOT EXISTS site_name TEXT,
    ADD COLUMN IF NOT EXISTS site_id TEXT,
    ADD COLUMN IF NOT EXISTS task TEXT;

-- =========================================================================
-- 2. Migrate existing data from entry_a/entry_b columns into JSONB
-- =========================================================================
UPDATE data_staging.stg_timer_duplicate_reviews
SET entries = jsonb_build_array(
    jsonb_build_object(
        'label', 'A',
        'end_time', entry_a_end_time,
        'duration_min', entry_a_duration,
        'site_name', entry_a_site_name,
        'task', entry_a_task
    ),
    jsonb_build_object(
        'label', 'B',
        'end_time', entry_b_end_time,
        'duration_min', entry_b_duration,
        'site_name', entry_b_site_name,
        'task', entry_b_task
    )
)
WHERE entries IS NULL;

-- Migrate rejected natural keys for resolved entries
UPDATE data_staging.stg_timer_duplicate_reviews
SET rejected_entries = jsonb_build_array(
    jsonb_build_object(
        'end_time', rejected_end_time,
        'duration_min', rejected_duration
    )
)
WHERE status IN ('resolved', 'auto_resolved')
  AND rejected_end_time IS NOT NULL
  AND rejected_entries IS NULL;

-- =========================================================================
-- 3. Drop old columns and update constraint
-- =========================================================================
ALTER TABLE data_staging.stg_timer_duplicate_reviews
    DROP COLUMN IF EXISTS entry_a_end_time,
    DROP COLUMN IF EXISTS entry_a_duration,
    DROP COLUMN IF EXISTS entry_a_site_name,
    DROP COLUMN IF EXISTS entry_a_task,
    DROP COLUMN IF EXISTS entry_b_end_time,
    DROP COLUMN IF EXISTS entry_b_duration,
    DROP COLUMN IF EXISTS entry_b_site_name,
    DROP COLUMN IF EXISTS entry_b_task,
    DROP COLUMN IF EXISTS rejected_end_time,
    DROP COLUMN IF EXISTS rejected_duration;

-- Widen selected_entry to allow any letter A-Z
ALTER TABLE data_staging.stg_timer_duplicate_reviews
    DROP CONSTRAINT IF EXISTS stg_timer_duplicate_reviews_selected_entry_check;
ALTER TABLE data_staging.stg_timer_duplicate_reviews
    ADD CONSTRAINT stg_timer_duplicate_reviews_selected_entry_check
    CHECK (selected_entry ~ '^[A-Z]$');

-- Make entries NOT NULL now that data is migrated
ALTER TABLE data_staging.stg_timer_duplicate_reviews
    ALTER COLUMN entries SET NOT NULL;

-- =========================================================================
-- 4. Updated RPC to rebuild clean table from JSONB arrays
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

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
        -- For unresolved duplicates (pending/notified), keep only the entry
        -- with the latest end_time per group. Exclude all others.
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
              -- This row matches one of the entries in this group...
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              -- ...and it's NOT the one with the latest end_time
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        );
END;
$$;

-- =========================================================================
-- 5. Update schema metadata
-- =========================================================================
UPDATE agent.schema_metadata
SET description = 'JSONB array of all entries in the duplicate group. Each element: {label, end_time, duration_min, site_name, task}. Labels are A, B, C, ... sorted by duration ascending.'
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_timer_duplicate_reviews'
  AND column_name = 'selected_entry';

-- Update selected_entry description
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
VALUES
    ('data_staging', 'stg_timer_duplicate_reviews', 'entries',
     'JSONB array of all entries in the duplicate group. Each element: {label, end_time, duration_min, site_name, task}. Labels are A, B, C, ... sorted by duration ascending.'),
    ('data_staging', 'stg_timer_duplicate_reviews', 'rejected_entries',
     'JSONB array of rejected entry natural keys: [{end_time, duration_min}, ...]. Used by rebuild_timer_clean() to exclude rows from the clean table.')
ON CONFLICT DO NOTHING;

-- Remove old column metadata
DELETE FROM agent.schema_metadata
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_timer_duplicate_reviews'
  AND column_name = 'rejected_end_time';
