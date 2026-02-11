-- Migration 019: Add asset_did to stg_timer_activities and stg_qa_form
--
-- asset_id (human-readable site ID like "ATL001") can change over time,
-- but asset_did (immutable Swift API identifier) never changes.
-- Timer uses append mode so old rows persist — stale asset_id values
-- would lose their link without asset_did.
--
-- The Swift API timer/form endpoints don't return Asset_DID, so we
-- look it up from stg_assets which has both asset_id and asset_did.

-- ============================================================
-- 1. Add asset_did column to both tables
-- ============================================================

ALTER TABLE data_staging.stg_timer_activities
    ADD COLUMN IF NOT EXISTS asset_did TEXT;

ALTER TABLE data_staging.stg_qa_form
    ADD COLUMN IF NOT EXISTS asset_did TEXT;

-- ============================================================
-- 2. Add indexes on the new columns
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_asset_did
    ON data_staging.stg_timer_activities(asset_did);

CREATE INDEX IF NOT EXISTS idx_stg_qa_form_asset_did
    ON data_staging.stg_qa_form(asset_did);

-- ============================================================
-- 3. One-time backfill for existing data
-- ============================================================

-- Timer Pass 1: join on project_did + site_id = asset_id
UPDATE data_staging.stg_timer_activities t
SET asset_did = sub.asset_did
FROM (
    SELECT DISTINCT ON (project_did, asset_id) project_did, asset_id, asset_did
    FROM data_staging.stg_assets
    WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
    ORDER BY project_did, asset_id, asset_did
) sub
WHERE t.project_did = sub.project_did
  AND t.site_id = sub.asset_id
  AND t.asset_did IS NULL;

-- Timer Pass 2: fallback on project_did + site_name = asset_name
UPDATE data_staging.stg_timer_activities t
SET asset_did = sub.asset_did
FROM (
    SELECT DISTINCT ON (project_did, asset_name) project_did, asset_name, asset_did
    FROM data_staging.stg_assets
    WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
    ORDER BY project_did, asset_name, asset_did
) sub
WHERE t.asset_did IS NULL
  AND t.project_did = sub.project_did
  AND t.site_name = sub.asset_name;

-- QA Form Pass 1: join on site_id = asset_id
UPDATE data_staging.stg_qa_form q
SET asset_did = sub.asset_did
FROM (
    SELECT DISTINCT ON (asset_id) asset_id, asset_did
    FROM data_staging.stg_assets
    WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
    ORDER BY asset_id, project_did
) sub
WHERE q.site_id = sub.asset_id
  AND q.asset_did IS NULL;

-- QA Form Pass 2: fallback on site_name = asset_name
UPDATE data_staging.stg_qa_form q
SET asset_did = sub.asset_did
FROM (
    SELECT DISTINCT ON (asset_name) asset_name, asset_did
    FROM data_staging.stg_assets
    WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
    ORDER BY asset_name, project_did
) sub
WHERE q.asset_did IS NULL
  AND q.site_name = sub.asset_name;

-- ============================================================
-- 4a. Persistent lookup table for QA form asset_did mappings
--     QA form uses truncate+reload, so asset_did is wiped each run.
--     This table preserves (site_id -> asset_did) across runs so
--     mappings survive even if site_id/site_name change in stg_assets.
-- ============================================================

CREATE TABLE IF NOT EXISTS data_staging.qa_form_asset_did_lookup (
    site_id TEXT NOT NULL PRIMARY KEY,
    site_name TEXT,
    asset_did TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qa_form_lookup_site_name
    ON data_staging.qa_form_asset_did_lookup(site_name);

GRANT ALL ON data_staging.qa_form_asset_did_lookup TO service_role;
GRANT SELECT ON data_staging.qa_form_asset_did_lookup TO anon, authenticated;

-- ============================================================
-- 4b. RPC function for ongoing backfill (called after each pipeline run)
--     All passes are NULL-only — once asset_did is set, never overwritten.
--
--     QA Form flow:
--       Pass 0: restore from lookup table (survives truncate+reload)
--       Pass 1-3: fill remaining NULLs from stg_assets
--       Save: update lookup table with any new mappings
--
--     Timer flow:
--       Pass 1-3: fill NULLs from stg_assets (append mode, no lookup needed)
-- ============================================================

CREATE OR REPLACE FUNCTION data_staging.backfill_asset_did()
RETURNS TABLE(timer_updated BIGINT, qa_form_updated BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_timer BIGINT := 0;
    v_qa    BIGINT := 0;
    v_rows  BIGINT;
BEGIN
    SET statement_timeout = '120s';

    -- ============================================================
    -- TIMER: Pass 1 — match on project_did + site_id = asset_id
    -- Only fills NULLs — once asset_did is set, it's preserved
    -- (asset_id/asset_name may change but asset_did is immutable)
    -- ============================================================
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (project_did, asset_id) project_did, asset_id, asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
        ORDER BY project_did, asset_id, asset_did
    ) sub
    WHERE t.asset_did IS NULL
      AND t.project_did = sub.project_did
      AND t.site_id = sub.asset_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- ============================================================
    -- TIMER: Pass 2 — fallback on project_did + site_name = asset_name
    -- ============================================================
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (project_did, asset_name) project_did, asset_name, asset_did
        FROM data_staging.stg_assets
        WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
        ORDER BY project_did, asset_name, asset_did
    ) sub
    WHERE t.asset_did IS NULL
      AND t.project_did = sub.project_did
      AND t.site_name = sub.asset_name;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- ============================================================
    -- TIMER: Pass 3 — extract 7-8 digit FA number from site_id
    -- Excludes junk number 00000000
    -- ============================================================
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (fa_num)
               fa_num, asset_did
        FROM (
            SELECT (regexp_match(asset_id, E'/([0-9]{7,8})/'))[1] as fa_num, asset_did
            FROM data_staging.stg_assets
            WHERE asset_id ~ E'/[0-9]{7,8}/'
        ) x
        WHERE fa_num IS NOT NULL AND fa_num <> '00000000'
        ORDER BY fa_num, asset_did
    ) sub
    WHERE t.asset_did IS NULL
      AND (regexp_match(t.site_id, E'/([0-9]{7,8})/'))[1] = sub.fa_num;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- ============================================================
    -- QA FORM: Pass 0 — restore from persistent lookup table
    -- Recovers asset_did lost during truncate+reload, even if
    -- site_id/site_name changed in stg_assets since last run
    -- ============================================================
    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_id IS NOT NULL AND q.site_id <> ''
      AND q.site_id = l.site_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- Pass 0b — fallback: match by site_name from lookup
    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_name IS NOT NULL AND q.site_name <> ''
      AND q.site_name = l.site_name;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- ============================================================
    -- QA FORM: Pass 1 — match on site_id = asset_id
    -- ============================================================
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (asset_id) asset_id, asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
        ORDER BY asset_id, project_did
    ) sub
    WHERE q.asset_did IS NULL
      AND q.site_id = sub.asset_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- ============================================================
    -- QA FORM: Pass 2 — fallback on site_name = asset_name
    -- ============================================================
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (asset_name) asset_name, asset_did
        FROM data_staging.stg_assets
        WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
        ORDER BY asset_name, project_did
    ) sub
    WHERE q.asset_did IS NULL
      AND q.site_name = sub.asset_name;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- ============================================================
    -- QA FORM: Pass 3 — extract 7-8 digit FA number from site_id
    -- ============================================================
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (fa_num)
               fa_num, asset_did
        FROM (
            SELECT (regexp_match(asset_id, E'/([0-9]{7,8})/'))[1] as fa_num, asset_did
            FROM data_staging.stg_assets
            WHERE asset_id ~ E'/[0-9]{7,8}/'
        ) x
        WHERE fa_num IS NOT NULL AND fa_num <> '00000000'
        ORDER BY fa_num, asset_did
    ) sub
    WHERE q.asset_did IS NULL
      AND (regexp_match(q.site_id, E'/([0-9]{7,8})/'))[1] = sub.fa_num;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- ============================================================
    -- QA FORM: Save — persist current mappings for next run
    -- UPSERT so new site_ids get added, existing ones stay
    -- ============================================================
    INSERT INTO data_staging.qa_form_asset_did_lookup (site_id, site_name, asset_did)
    SELECT DISTINCT ON (site_id) site_id, site_name, asset_did
    FROM data_staging.stg_qa_form
    WHERE asset_did IS NOT NULL AND site_id IS NOT NULL AND site_id <> ''
    ORDER BY site_id, loaded_at DESC
    ON CONFLICT (site_id) DO UPDATE
        SET site_name = EXCLUDED.site_name,
            updated_at = NOW();

    RETURN QUERY SELECT v_timer, v_qa;
END;
$$;

-- Grant execute to service_role
GRANT EXECUTE ON FUNCTION data_staging.backfill_asset_did() TO service_role;

-- ============================================================
-- 5. Schema metadata for new columns
-- ============================================================

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
    ('data_staging', 'stg_timer_activities', 'asset_did',
     'Immutable Swift API asset identifier, backfilled from stg_assets',
     'Stable foreign key to stg_assets — unlike site_id (= asset_id) which can change over time. Populated by backfill_asset_did() RPC after each pipeline run.'),

    ('data_staging', 'stg_qa_form', 'asset_did',
     'Immutable Swift API asset identifier, backfilled from stg_assets',
     'Stable foreign key to stg_assets — unlike site_id (= asset_id) which can change over time. Populated by backfill_asset_did() RPC after each pipeline run.')
ON CONFLICT DO NOTHING;

-- ============================================================
-- 6. Update table-level metadata with asset_did relationship
-- ============================================================

UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)', 'stg_assets (via asset_did)', 'stg_projects (via project_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name IS NULL;

UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)', 'stg_assets (via asset_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_qa_form' AND column_name IS NULL;
