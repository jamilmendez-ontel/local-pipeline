-- 209: stg_assets.asset_identifier — surface the Swift asset path in staging
-- Context: timer→revenue mapping (docs/specs/timer-revenue-market-crosswalk.md).
-- The fine market lives in the asset path (data_raw.raw_assets.asset_identifier,
-- e.g. 'CNB/New-Tech Construction/VZW/FL/Embedded/16285600/Dec 2024'); staging
-- dropped it. stg_assets is fully refreshed each run by transform_assets(), so
-- the ongoing population happens in enrich_stg_assets_with_status() (transform.py),
-- which already re-enriches asset_status from raw_assets after every assets
-- extract. This migration adds the column and does the initial backfill.

ALTER TABLE data_staging.stg_assets ADD COLUMN IF NOT EXISTS asset_identifier text;

COMMENT ON COLUMN data_staging.stg_assets.asset_identifier IS
  'Swift asset path (Site ID): GC[/Sub]/Carrier/Market/Scope/ProjectNumber/Date. '
  'Source data_raw.raw_assets.asset_identifier; re-enriched every assets extract. '
  'Fine-market source for reference.ref_market_bucket_crosswalk.';

WITH src AS (
    SELECT DISTINCT ON (project_did, asset_did)
           project_did, asset_did, asset_identifier
    FROM data_raw.raw_assets
    WHERE asset_identifier IS NOT NULL
    ORDER BY project_did, asset_did, loaded_at DESC
)
UPDATE data_staging.stg_assets s
SET asset_identifier = src.asset_identifier
FROM src
WHERE s.project_did = src.project_did
  AND s.asset_did = src.asset_did
  AND s.asset_identifier IS DISTINCT FROM src.asset_identifier;
