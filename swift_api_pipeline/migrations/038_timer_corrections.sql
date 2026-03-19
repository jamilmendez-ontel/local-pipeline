-- Migration 038: Timer Duration Corrections
--
-- Techs can report wrong timer durations via a Google Form. Corrections
-- are stored here and applied by rebuild_timer_clean() — the original
-- stg_timer_activities is never modified.
--
-- entry_id is a 12-char md5 hash of the full natural key including
-- end_time and duration_min (unlike duplicate reviews which group by
-- start_time only). This uniquely identifies a single timer row.

-- =========================================================================
-- 1. Corrections table
-- =========================================================================
CREATE TABLE IF NOT EXISTS data_staging.stg_timer_corrections (
    id                    BIGSERIAL PRIMARY KEY,
    entry_id              TEXT NOT NULL UNIQUE,  -- 12-char hex hash

    -- Entry identification (natural keys)
    project_did           TEXT NOT NULL,
    project               TEXT,
    user_email            TEXT NOT NULL,
    start_time            TIMESTAMPTZ NOT NULL,
    site_name             TEXT,
    site_id               TEXT,
    task                  TEXT,
    end_time              TIMESTAMPTZ,
    original_duration_min NUMERIC,

    -- Correction
    corrected_duration_min NUMERIC NOT NULL,
    corrected_end_time     TIMESTAMPTZ NOT NULL,  -- start_time + corrected_duration
    reason                 TEXT,

    -- Status tracking
    status                TEXT NOT NULL DEFAULT 'corrected'
                          CHECK (status IN ('corrected')),

    corrected_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timer_corrections_entry_id
    ON data_staging.stg_timer_corrections (entry_id);

CREATE INDEX IF NOT EXISTS idx_timer_corrections_natural_key
    ON data_staging.stg_timer_corrections (project_did, user_email, start_time);

-- =========================================================================
-- 2. Update rebuild_timer_clean() to apply corrections after dedup
-- =========================================================================
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
RETURNS void
LANGUAGE plpgsql
SET statement_timeout = '300s'
AS $$
BEGIN
    -- Step 1: Truncate clean table
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 2: Insert from staging, excluding rejected duplicates
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
-- 3. Schema metadata for DARA
-- =========================================================================
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
VALUES
    ('data_staging', 'stg_timer_corrections', 'entry_id',
     '12-char hex md5 hash of (project_did|user_email|start_time|site_name|site_id|task|end_time|duration_min). Uniquely identifies one timer entry.'),
    ('data_staging', 'stg_timer_corrections', 'corrected_duration_min',
     'Tech-reported correct duration in minutes. Replaces original_duration_min in the clean table.'),
    ('data_staging', 'stg_timer_corrections', 'corrected_end_time',
     'Recalculated as start_time + corrected_duration_min. Replaces original end_time in the clean table.'),
    ('data_staging', 'stg_timer_corrections', 'reason',
     'Why the duration was wrong: Ended early, Forgot to stop timer, Wrong duration logged, Other'),
    ('data_staging', 'stg_timer_corrections', 'status',
     'Always ''corrected''. Corrections are applied by rebuild_timer_clean() RPC.')
ON CONFLICT DO NOTHING;
