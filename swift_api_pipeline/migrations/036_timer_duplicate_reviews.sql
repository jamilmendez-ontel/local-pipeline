-- Migration 036: Timer Duplicate Review system
--
-- 1. Review tracking table (tracks detected duplicates + tech selections)
-- 2. Clean timer table (deduplicated copy of stg_timer_activities)
-- 3. RPC to rebuild the clean table from staging + resolved reviews
--
-- stg_timer_activities is NEVER modified. All deduplication happens in the
-- clean table by excluding rows that match rejected natural keys.
--
-- IMPORTANT: We use natural keys (end_time + duration) instead of surrogate IDs
-- because the timer pipeline DELETEs + re-INSERTs the whole month each run,
-- which reassigns auto-increment IDs nightly.

-- =========================================================================
-- 1. Review tracking table
-- =========================================================================
CREATE TABLE IF NOT EXISTS data_staging.stg_timer_duplicate_reviews (
    id              BIGSERIAL PRIMARY KEY,
    group_id        TEXT NOT NULL UNIQUE,  -- md5 hash of (project_did || user_email || start_time), 12 hex chars

    -- Shared natural key (same for both entries in the group)
    project_did     TEXT NOT NULL,
    project         TEXT,
    user_email      TEXT NOT NULL,
    start_time      TIMESTAMPTZ NOT NULL,

    -- Entry A (shorter duration or earlier end_time)
    entry_a_end_time TIMESTAMPTZ,
    entry_a_duration NUMERIC,
    entry_a_site_name TEXT,
    entry_a_task     TEXT,

    -- Entry B (longer duration or later end_time)
    entry_b_end_time TIMESTAMPTZ,
    entry_b_duration NUMERIC,
    entry_b_site_name TEXT,
    entry_b_task     TEXT,

    -- Resolution
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'notified', 'resolved', 'auto_resolved')),
    selected_entry  TEXT CHECK (selected_entry IN ('A', 'B')),

    -- Natural key of the rejected entry (for clean table exclusion)
    rejected_end_time   TIMESTAMPTZ,
    rejected_duration   NUMERIC,

    -- Notification tracking
    notified_at     TIMESTAMPTZ,
    reminder_count  INT NOT NULL DEFAULT 0,
    last_reminder_at TIMESTAMPTZ,

    -- Resolution tracking
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,  -- 'tech' or 'auto'

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_timer_dup_reviews_status
    ON data_staging.stg_timer_duplicate_reviews (status);
CREATE INDEX IF NOT EXISTS idx_timer_dup_reviews_user_email
    ON data_staging.stg_timer_duplicate_reviews (user_email);
CREATE INDEX IF NOT EXISTS idx_timer_dup_reviews_notified_at
    ON data_staging.stg_timer_duplicate_reviews (notified_at);

-- =========================================================================
-- 2. Clean timer table — same structure as stg_timer_activities
-- =========================================================================
CREATE TABLE IF NOT EXISTS data_staging.stg_timer_activities_clean (
    LIKE data_staging.stg_timer_activities INCLUDING ALL
);

COMMENT ON TABLE data_staging.stg_timer_activities_clean IS
    'Deduplicated copy of stg_timer_activities. Rebuilt by rebuild_timer_clean() '
    'after each review cycle. Excludes rows matching rejected natural keys from '
    'stg_timer_duplicate_reviews. For unresolved duplicates, keeps the row with '
    'the latest end_time.';

-- =========================================================================
-- 3. RPC to rebuild the clean table
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
            FROM data_staging.stg_timer_duplicate_reviews r
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_end_time IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = r.start_time
              AND t.end_time IS NOT DISTINCT FROM r.rejected_end_time
              AND t.duration_min IS NOT DISTINCT FROM r.rejected_duration
        )
        -- For unresolved duplicates (pending/notified), keep only the row
        -- with the latest end_time per group. Exclude the shorter one.
        AND NOT EXISTS (
            SELECT 1
            FROM data_staging.stg_timer_duplicate_reviews r
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = r.start_time
              AND (
                  -- This row matches entry_a, and entry_b has later/equal end_time → exclude
                  (t.end_time IS NOT DISTINCT FROM r.entry_a_end_time
                   AND t.duration_min IS NOT DISTINCT FROM r.entry_a_duration
                   AND r.entry_b_end_time >= COALESCE(r.entry_a_end_time, '-infinity'::timestamptz))
                  OR
                  -- This row matches entry_b, and entry_a has strictly later end_time → exclude
                  (t.end_time IS NOT DISTINCT FROM r.entry_b_end_time
                   AND t.duration_min IS NOT DISTINCT FROM r.entry_b_duration
                   AND r.entry_a_end_time > COALESCE(r.entry_b_end_time, '-infinity'::timestamptz))
              )
        );
END;
$$;

-- =========================================================================
-- 4. Schema metadata for DARA
-- =========================================================================
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
VALUES
    ('data_staging', 'stg_timer_duplicate_reviews', NULL,
     'Tracks duplicate timer entries where the same user has multiple rows for the same start_time. Techs select which entry to keep via email/form; rejected entries are excluded from stg_timer_activities_clean by natural key match.'),
    ('data_staging', 'stg_timer_duplicate_reviews', 'group_id',
     'Unique 12-char hex hash identifying the duplicate group (md5 of project_did + user_email + start_time)'),
    ('data_staging', 'stg_timer_duplicate_reviews', 'status',
     'Review status: pending (detected), notified (email sent), resolved (tech chose), auto_resolved (7-day timeout, kept latest end_time)'),
    ('data_staging', 'stg_timer_duplicate_reviews', 'selected_entry',
     'Which entry the tech chose to keep: A (earlier-loaded) or B (later-loaded)'),
    ('data_staging', 'stg_timer_duplicate_reviews', 'rejected_end_time',
     'end_time of the rejected entry — used with (project_did, user_email, start_time, rejected_duration) to match and exclude rows from the clean table'),
    ('data_staging', 'stg_timer_activities_clean', NULL,
     'Deduplicated copy of stg_timer_activities. Excludes rejected duplicates based on stg_timer_duplicate_reviews natural key matching. Rebuilt by data_staging.rebuild_timer_clean() after each review cycle. Use this table instead of stg_timer_activities for reporting.')
ON CONFLICT DO NOTHING;
