-- Migration 104: asset_did matching = combined (path + name) key, no project scope
--
-- FOLLOW-UP to migration 100. After 100 (name-first + uniqueness guards, all
-- scoped to project_did), two classes of issue remained on the TIMER side:
--
--   1. Name-blind fallback. When the exact name match missed (because the tech
--      typed a qualifier like "(Civil)" or a scope variant, or whitespace), the
--      row fell through to the site_id PATH pass. The path is a shared batch
--      folder, so it stamped the wrong asset (e.g. "9JK2240A (Civil)" -> the
--      non-civil parent "9JK2240A"). 401 timer sites / ~1,200 rows were mislinked
--      this way, all to assets whose name does NOT match what the tech typed.
--
--   2. Project scope was too strict. Assets sometimes move between projects, so a
--      site that genuinely matches an asset registered under a different project
--      was left NULL (or grabbed off the path). ~1,665 rows.
--
-- ROOT PRINCIPLE (unchanged): only assign asset_did when the match is UNAMBIGUOUS,
-- and only to an asset whose NAME actually matches what the technician typed.
-- Verified on live data: site_name and site_id are each individually ambiguous
-- (names recur across projects/efforts; paths are shared batch folders), but the
-- COMBINATION (path + name) is reliable, and an exact name match is the trustworthy
-- disambiguator. There is NO case where a single timer entry needs a name-blind
-- path/FA guess; such guesses only ever produced wrong links.
--
-- NEW TIMER LOGIC (replaces the old 3 passes):
--   Pass A: asset where asset_id = site_id AND TRIM(asset_name) = TRIM(site_name),
--           assigned only if that (path, name) maps to a single asset_did.
--           Path breaks name ties (same name, different effort); name breaks path
--           ties (shared batch folder).
--   Pass B: asset where TRIM(asset_name) = TRIM(site_name), assigned only if that
--           name maps to a single asset_did globally. Catches cross-project moves
--           and shared-path sites that Pass A's exact path didn't cover.
--   No site_id-path-only pass. No FA-number pass. No project_did scope.
--   Anything not uniquely resolved by A or B is left NULL (honest "unknown").
--
-- Simulated impact vs current data (base stg_timer_activities, 373,627 rows):
--   +1,665 newly linked (cross-project recoveries), 1,421 wrong links -> NULL,
--   11 re-pointed to the exact-name asset, 99.2% unchanged, net coverage +244.
--
-- QA FORM gets the SAME treatment (stg_qa_form has site_id + site_name, no project_did):
--   Pass A: asset_id = site_id AND TRIM(asset_name) = TRIM(site_name), unique did.
--   Pass B: TRIM(asset_name) = TRIM(site_name), unique did globally.
-- The old QA logic used "take the first" (DISTINCT ON, no uniqueness guard) plus the
-- qa_form_asset_did_lookup restore (keyed by the weak site_id path). Both are removed.
-- Simulated QA impact: 37,208 wrong links corrected (0 regressions), 1,534 wrong links
-- and 1,769 ambiguous name-correct links -> NULL (honest unknown). The lookup table is
-- left in place but no longer read or written (nothing else references it).
--
-- REPAIR STEPS to run AFTER this migration (re-derive, like migration 100 did):
--   UPDATE data_staging.stg_timer_activities SET asset_did = NULL;
--   UPDATE data_staging.stg_qa_form          SET asset_did = NULL;
--   SELECT data_staging.backfill_asset_did();
--   SELECT data_staging.rebuild_timer_clean();

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

    -- TIMER: Pass A -- path AND name agree, unambiguous (strongest key, no scope)
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT asset_id, TRIM(asset_name) AS aname, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_name IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY asset_id, TRIM(asset_name)
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE t.asset_did IS NULL
      AND t.site_id = sub.asset_id
      AND TRIM(t.site_name) = sub.aname;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- TIMER: Pass B -- name match, globally unique (cross-project moves + shared paths)
    UPDATE data_staging.stg_timer_activities t
    SET asset_did = sub.asset_did
    FROM (
        SELECT TRIM(asset_name) AS aname, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY TRIM(asset_name)
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE t.asset_did IS NULL
      AND TRIM(t.site_name) = sub.aname;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_timer := v_timer + v_rows;

    -- QA FORM: Pass A -- path AND name agree, unambiguous (strongest key, no scope)
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT asset_id, TRIM(asset_name) AS aname, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_id IS NOT NULL AND asset_name IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY asset_id, TRIM(asset_name)
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE q.asset_did IS NULL
      AND q.site_id = sub.asset_id
      AND TRIM(q.site_name) = sub.aname;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    -- QA FORM: Pass B -- name match, globally unique (cross-project moves + shared paths)
    UPDATE data_staging.stg_qa_form q
    SET asset_did = sub.asset_did
    FROM (
        SELECT TRIM(asset_name) AS aname, MIN(asset_did) AS asset_did
        FROM data_staging.stg_assets
        WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
        GROUP BY TRIM(asset_name)
        HAVING COUNT(DISTINCT asset_did) = 1
    ) sub
    WHERE q.asset_did IS NULL
      AND TRIM(q.site_name) = sub.aname;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_qa := v_qa + v_rows;

    RETURN QUERY SELECT v_timer, v_qa;
END;
$function$;
