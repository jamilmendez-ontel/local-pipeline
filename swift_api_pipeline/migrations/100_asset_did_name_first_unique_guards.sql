-- Migration 100: asset_did robust matching (name-first + uniqueness guards on every pass)
--
-- FOLLOW-UP to migration 099. After 099 fixed the catastrophic path-collision
-- (one did smeared across 82 sites), a smaller residual remained: a handful of
-- dids still linked genuinely-different sites, from THREE distinct causes:
--   1. Pass 3 FA-number collisions (different sites sharing a 7-8 digit number).
--   2. Cross-org shared batch paths matched by Pass 1 when the asset side
--      happened to have a single asset for that path (e.g. TSC T-Mobile GA
--      Overlay sites 9AT*, A2*, DA*).
--   3. site_name itself ambiguous within a project (mapped to >1 asset_did),
--      where DISTINCT ON picked one arbitrarily.
--
-- ROOT PRINCIPLE: only assign asset_did when the match key is UNAMBIGUOUS.
-- The most reliable key Swift gives us on a timer row is site_name (unique per
-- site), so match on that FIRST; use site_id path and FA number only to fill
-- gaps, and only when THEY are unambiguous too. Genuinely-ambiguous rows are
-- left NULL (honest "unknown") instead of getting a wrong link.
--
-- Changes vs 099 (TIMER passes only; QA passes unchanged from 099):
--   - Order is now: Pass 1 = site_name (was Pass 2), Pass 2 = site_id path
--     (was Pass 1), Pass 3 = FA number.
--   - Every pass guarded with GROUP BY ... HAVING COUNT(DISTINCT asset_did) = 1.
--
-- NOTE: QA form still carries the same class of issue plus a poisoned
-- qa_form_asset_did_lookup table (Pass 0). That remains a separate follow-up;
-- this migration does not change QA behavior.

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

    -- TIMER: Pass 1 -- site_name = asset_name, unique within project (most reliable)
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT project_did, asset_name, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY project_did, asset_name
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE t.asset_did IS NULL
      AND t.project_did = sub.project_did
      AND t.site_name = sub.asset_name;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- TIMER: Pass 2 -- site_id = asset_id path, unique within project (gap fill)
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

    -- TIMER: Pass 3 -- FA number from site_id, unique globally (last-resort gap fill)
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT fa_num, MIN(asset_did) AS asset_did
        FROM (
            SELECT (regexp_match(asset_id, E'/([0-9]{7,8})/'))[1] as fa_num, asset_did
            FROM data_staging.stg_assets
            WHERE asset_id ~ E'/[0-9]{7,8}/' AND asset_did IS NOT NULL
        ) x
        WHERE fa_num IS NOT NULL AND fa_num <> '00000000'
        GROUP BY fa_num
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE t.asset_did IS NULL
      AND (regexp_match(t.site_id, E'/([0-9]{7,8})/'))[1] = sub.fa_num;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- QA FORM: unchanged from migration 099 (separate follow-up pending)
    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_id IS NOT NULL AND q.site_id <> ''
      AND q.site_id = l.site_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    UPDATE data_staging.stg_qa_form q
    SET asset_did = l.asset_did
    FROM data_staging.qa_form_asset_did_lookup l
    WHERE q.asset_did IS NULL
      AND q.site_name IS NOT NULL AND q.site_name <> ''
      AND q.site_name = l.site_name;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

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

    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT DISTINCT ON (fa_num) fa_num, asset_did
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
