-- Migration 022: Increase RPC statement timeouts from 120s to 300s
--
-- Problem: Function-level SET statement_timeout = '120s' overrides the
-- session-level 300s set by db.py. The aggregate_assets RPC processes
-- 2.2M rows and needs ~3-5 minutes. The backfill_asset_did RPC runs
-- multiple UPDATE passes across 300K+ rows.
--
-- With PostgREST (old architecture), 120s was fine because PostgREST
-- default was 8s and we needed to override it upward. With asyncpg
-- (new architecture), the session already has 300s and these function-level
-- SETs were reducing it back to 120s.

-- 1. Fix aggregate_assets_from_raw: function-level attribute
ALTER FUNCTION data_raw.aggregate_assets_from_raw(text) SET statement_timeout = '300s';

-- 2. Fix backfill_asset_did: internal SET inside function body
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
    SET statement_timeout = '300s';

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
