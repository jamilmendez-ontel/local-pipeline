-- Migration 024: Increase RPC statement timeouts from 300s to 600s
--
-- Problem: aggregate_assets_from_raw processes 2.2M+ raw_asset_tasks rows
-- and can take >5 minutes under concurrent load (observed 2026-02-18:
-- "connection was closed in the middle of operation" after ~5 min).
-- The function-level SET statement_timeout = '300s' (from migration 022)
-- overrides the session's 600s, so the function still times out.
--
-- Also bumps backfill_asset_did for consistency — it runs multiple
-- UPDATE passes across 300K+ rows.

-- 1. aggregate_assets_from_raw: function-level attribute
ALTER FUNCTION data_raw.aggregate_assets_from_raw(text) SET statement_timeout = '600s';

-- 2. backfill_asset_did: internal SET inside function body
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
    SET statement_timeout = '600s';

    -- TIMER: Pass 1 — match on project_did + site_id = asset_id
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

    -- TIMER: Pass 2 — fallback on project_did + site_name = asset_name
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

    -- TIMER: Pass 3 — extract 7-8 digit FA number from site_id
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

    -- QA FORM: Pass 0 — restore from persistent lookup table
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

    -- QA FORM: Pass 1 — match on site_id = asset_id
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

    -- QA FORM: Pass 2 — fallback on site_name = asset_name
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

    -- QA FORM: Pass 3 — extract 7-8 digit FA number from site_id
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

    -- QA FORM: Save — persist current mappings for next run
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
