-- Migration 026: Add carrier_group column to stg_assets
-- Populated from carrier_group_lookup by matching search_term against asset_id

ALTER TABLE data_staging.stg_assets ADD COLUMN IF NOT EXISTS carrier_group TEXT;

CREATE INDEX IF NOT EXISTS idx_stg_assets_carrier_group
    ON data_staging.stg_assets(carrier_group);

-- Backfill: first-match-wins by match_order
UPDATE data_staging.stg_assets a
SET carrier_group = sub.carrier_group
FROM (
    SELECT DISTINCT ON (a2.asset_did)
        a2.asset_did,
        cg.carrier_group
    FROM data_staging.stg_assets a2
    JOIN data_staging.carrier_group_lookup cg
        ON a2.asset_id ILIKE '%' || cg.search_term || '%'
    ORDER BY a2.asset_did, cg.match_order
) sub
WHERE a.asset_did = sub.asset_did;
