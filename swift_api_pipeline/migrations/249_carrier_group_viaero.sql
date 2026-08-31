-- 249: Viaero -> Verizon carrier group (HR/app grouping), companion to 247/248.
-- Applied to OntelDB via Supabase MCP 2026-08-27 (name 249_carrier_group_viaero).
-- Jamil 2026-08-27: "viaero is treated as VZW small cell", so it joins the Verizon
-- carrier_group like every VZW asset. reference.ref_carrier_groups is the ILIKE search-term
-- lookup transform.py uses to backfill data_staging.stg_assets.carrier_group (only rows still
-- NULL are touched, so existing groups are never rewritten). The 309 Viaero assets had
-- carrier_group NULL because no search term matched their path.
INSERT INTO reference.ref_carrier_groups (search_term, carrier_group, match_order)
SELECT 'Viaero', 'Verizon', 11
WHERE NOT EXISTS (SELECT 1 FROM reference.ref_carrier_groups WHERE search_term = 'Viaero');

-- Same backfill statement as transform.py (idempotent: WHERE carrier_group IS NULL).
WITH matched AS (
    SELECT DISTINCT ON (a.asset_did) a.asset_did, cg.carrier_group
    FROM data_staging.stg_assets a
    JOIN reference.ref_carrier_groups cg ON a.asset_id ILIKE '%' || cg.search_term || '%'
    WHERE a.carrier_group IS NULL
    ORDER BY a.asset_did, cg.match_order
)
UPDATE data_staging.stg_assets a
SET carrier_group = m.carrier_group
FROM matched m
WHERE a.asset_did = m.asset_did;

-- Verify: SELECT count(*) FROM data_staging.stg_assets WHERE asset_id ILIKE '%viaero%' AND carrier_group='Verizon'; -- 309
