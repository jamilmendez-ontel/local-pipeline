-- Migration 099: Fix asset_did Pass-1 path-collision bug
--
-- BUG (reported by Haj 2026-06-15):
--   asset_did '-OncdFSu8ipvF5eiGHss' (site FX4-FCS-03) was stamped onto 82
--   genuinely different sites in stg_timer_activities_clean.
--
-- ROOT CAUSE:
--   In FTTH-style projects the Swift `asset_id` field is a shared batch/folder
--   path (e.g. "MasTec/FL/MO9&10/FTTH/Phase 1/Mar 2026") that is identical
--   across every asset in the batch, NOT a per-site identifier. The timer's
--   `site_id` carries that same path. backfill_asset_did() Pass 1 joined
--   timer.site_id = asset.asset_id and used DISTINCT ON (project_did, asset_id)
--   to dedupe, collapsing ~88 distinct assets to a single arbitrarily-chosen
--   asset_did and stamping it on every timer row under that path. Pass 2
--   (site_name = asset_name), which would have matched correctly, never ran
--   because it only fills rows where asset_did IS NULL.
--
-- FIX:
--   Guard Pass 1 (timer + qa form) so it only matches when the asset_id path
--   resolves to EXACTLY ONE asset_did (unique within the project for timer,
--   unique globally for qa form). Ambiguous shared paths are skipped and fall
--   through to the site_name match, which is correct and unambiguous.
--
-- This migration only redefines backfill_asset_did(). Data correction
-- (NULL mismatched rows -> re-run backfill -> rebuild clean) is run separately.

CREATE OR REPLACE FUNCTION data_staging.backfill_asset_did()
 RETURNS TABLE(timer_updated bigint, qa_form_updated bigint)
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_timer BIGINT := 0;
    v_qa    BIGINT := 0;
    v_rows  BIGINT;
BEGIN
    SET statement_timeout = '600s';

    -- TIMER: Pass 1 -- match on project_did + site_id = asset_id
    -- GUARD: only when the (project_did, asset_id) path maps to exactly one
    -- asset_did. Shared FTTH batch paths are excluded and handled by Pass 2.
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT project_did, asset_id, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY project_did, asset_id
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE t.asset_did IS NULL
      AND t.project_did = sub.project_did
      AND t.site_id = sub.asset_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- TIMER: Pass 2 -- fallback on project_did + site_name = asset_name
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

    -- TIMER: Pass 3 -- extract 7-8 digit FA number from site_id
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

    -- QA FORM: Pass 0 -- restore from persistent lookup table
    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_id IS NOT NULL AND q.site_id <> ''
      AND q.site_id = l.site_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- Pass 0b -- fallback: match by site_name from lookup
    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_name IS NOT NULL AND q.site_name <> ''
      AND q.site_name = l.site_name;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- QA FORM: Pass 1 -- match on site_id = asset_id
    -- GUARD: only when asset_id maps to exactly one asset_did. Shared FTTH
    -- batch paths are excluded and handled by Pass 2 (site_name = asset_name).
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT asset_id, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY asset_id
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE q.asset_did IS NULL
      AND q.site_id = sub.asset_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- QA FORM: Pass 2 -- fallback on site_name = asset_name
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

    -- QA FORM: Pass 3 -- extract 7-8 digit FA number from site_id
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

    -- QA FORM: Save -- persist current mappings for next run
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
$function$;
