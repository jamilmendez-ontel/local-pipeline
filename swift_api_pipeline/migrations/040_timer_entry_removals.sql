-- Migration 040: Separate removals table + correction overrides removal
--
-- Removals (duplicate/wrong entries) are tracked separately from corrections
-- (duration fixes). Correction has higher priority — if an entry is both
-- removed AND corrected, the correction wins and the entry stays in the
-- clean table with the corrected duration.
--
-- Reverts stg_timer_corrections to 'corrected' only status.

-- =========================================================================
-- 1. Revert stg_timer_corrections to corrected-only
-- =========================================================================
-- Delete any 'removed' rows that may have been inserted during testing
DELETE FROM data_staging.stg_timer_corrections WHERE status = 'removed';

ALTER TABLE data_staging.stg_timer_corrections
    DROP CONSTRAINT IF EXISTS stg_timer_corrections_status_check;

ALTER TABLE data_staging.stg_timer_corrections
    ADD CONSTRAINT stg_timer_corrections_status_check
    CHECK (status IN ('corrected'));

-- Re-enforce NOT NULL on correction fields
-- (only run if no NULLs exist — safe since we deleted 'removed' rows above)
UPDATE data_staging.stg_timer_corrections
SET corrected_duration_min = 0, corrected_end_time = start_time
WHERE corrected_duration_min IS NULL;

ALTER TABLE data_staging.stg_timer_corrections
    ALTER COLUMN corrected_duration_min SET NOT NULL,
    ALTER COLUMN corrected_end_time SET NOT NULL;

-- =========================================================================
-- 2. Entry removals table
-- =========================================================================
CREATE TABLE IF NOT EXISTS data_staging.stg_timer_entry_removals (
    id              BIGSERIAL PRIMARY KEY,
    entry_id        TEXT NOT NULL UNIQUE,  -- 12-char hex hash (same as corrections)

    -- Entry identification (natural keys)
    project_did     TEXT NOT NULL,
    project         TEXT,
    user_email      TEXT NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,
    site_name       TEXT,
    site_id         TEXT,
    task            TEXT,
    end_time        TIMESTAMPTZ,
    duration_min    NUMERIC,

    reason          TEXT,
    removed_at      TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timer_entry_removals_entry_id
    ON data_staging.stg_timer_entry_removals (entry_id);

CREATE INDEX IF NOT EXISTS idx_timer_entry_removals_natural_key
    ON data_staging.stg_timer_entry_removals (project_did, user_email, start_time);

-- =========================================================================
-- 3. Update rebuild_timer_clean() — removals excluded UNLESS correction exists
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 2: Insert from staging, excluding rejected duplicates + removals
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
        -- Exclude removed entries UNLESS they also have a correction (correction wins)
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
              -- Correction overrides removal
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
    ('data_staging', 'stg_timer_entry_removals', 'entry_id',
     '12-char hex md5 hash — same algorithm as stg_timer_corrections. Uniquely identifies one timer entry to exclude from clean table.'),
    ('data_staging', 'stg_timer_entry_removals', 'reason',
     'Why the entry was removed. Optional free text from form.'),
    ('data_staging', 'stg_timer_entry_removals', 'duration_min',
     'Original duration of the removed entry. Used for natural key matching in rebuild_timer_clean().')
ON CONFLICT DO NOTHING;

-- Update corrections status description
UPDATE agent.schema_metadata
SET description = 'Always ''corrected''. Duration fixes only — removals are in stg_timer_entry_removals. Correction overrides removal.'
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_timer_corrections'
  AND column_name = 'status';
