-- 210: reference.ref_market_bucket_crosswalk + signature extraction function
-- Ports the RevMetrics workbook's col-67 "Market/Project" rules to the DB
-- (design + verified coverage: docs/specs/timer-revenue-market-crosswalk.md;
-- Excel source: reference/revenue-metrics/README.md).
-- Signature = carrier-anchored path segment + next two (FTTH: + next one, its
-- third segment is a date). Crosswalk maps signature → one of the 14
-- reference.ref_task_revenue_rates market buckets, or 'EXCLUDED'.
-- Verified 2026-08-02: rules classify 98.9% of asset-linked timer rows;
-- residue = 2023 TSC/Viaero/Nemont legacy outside the rate sheet.

-- 1. Signature extraction (port of Excel cCarrierMarket anchoring)
CREATE OR REPLACE FUNCTION reference.market_signature(p_path text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = ''
AS $$
WITH segs AS (
    SELECT seg, pos
    FROM unnest(string_to_array(p_path, '/')) WITH ORDINALITY AS t(seg, pos)
),
anchor AS (
    SELECT min(pos) AS cpos
    FROM segs
    WHERE seg ~* '^(VZW|AT&T|TMO|T-Mobile|DISH|FTTH|USCC|US Cellular|Westell|Gulf ?Services|AAHI)'
)
SELECT CASE
    WHEN a.cpos IS NULL THEN NULL
    WHEN (SELECT seg FROM segs WHERE pos = a.cpos) ~* '^FTTH' THEN
        (SELECT seg FROM segs WHERE pos = a.cpos)
        || COALESCE('/' || (SELECT seg FROM segs WHERE pos = a.cpos + 1), '')
    ELSE
        (SELECT seg FROM segs WHERE pos = a.cpos)
        || COALESCE('/' || (SELECT seg FROM segs WHERE pos = a.cpos + 1), '')
        || COALESCE('/' || (SELECT seg FROM segs WHERE pos = a.cpos + 2), '')
END
FROM anchor a
$$;

-- 2. Crosswalk table (one row per signature; manual patches become rows)
CREATE TABLE reference.ref_market_bucket_crosswalk (
    market_signature text PRIMARY KEY,
    market_bucket    text NOT NULL,  -- a ref_task_revenue_rates bucket, or 'EXCLUDED'
    source           text NOT NULL DEFAULT 'rule' CHECK (source IN ('rule', 'manual')),
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE reference.ref_market_bucket_crosswalk ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE reference.ref_market_bucket_crosswalk IS
  'Maps carrier-anchored asset-path signatures (reference.market_signature) to the 14 '
  'ref_task_revenue_rates market buckets, or EXCLUDED (legacy/non-billable). Port of the '
  'RevMetrics workbook col-67 rules. New unseen signatures must be seeded (QA query in '
  'docs/specs/timer-revenue-market-crosswalk.md) — an unmatched signature means unpriceable work.';

-- 3. Seed from every distinct signature observed in raw_assets
INSERT INTO reference.ref_market_bucket_crosswalk (market_signature, market_bucket, source, notes)
SELECT sig,
    CASE
        WHEN sig ~* 'VZW' AND sig ~* '(Embedded|Macro|NSB)'
             AND sig !~* 'Small Cell' AND sig !~* 'Decom'
             AND sig !~* '(AAHI|DONOR|MDU)' AND sig !~* 'Ground Scope'
                                                   THEN 'VZW Embedded / Macro'
        WHEN sig ~* 'VZW' AND sig ~* 'Small Cell'  THEN 'VZW Small Cell'
        WHEN sig ~* 'DONOR'                        THEN 'AAHI/DONOR'
        WHEN sig ~* 'MDU'                          THEN 'AAHI/MDU'
        WHEN sig ~* '(Ground Scope|Shelter Remodel|BidWalk)' THEN 'Ground Scope'
        WHEN sig ~* 'Decom'                        THEN 'VZW Decom'
        WHEN sig ~* '(Westell|CGC Remote)'         THEN 'Westell/CGC'
        WHEN sig ~* 'AT&T'                         THEN 'AT&T'
        WHEN sig ~* 'DISH'                         THEN 'Dish Wireless'
        WHEN sig ~* '(TMO|T-Mobile)'               THEN 'TMO'
        WHEN sig ~* 'FTTH' AND sig ~* '(Phase 1|Backbone)' THEN 'FTTH Phase 1'
        WHEN sig ~* 'FTTH' AND sig ~* 'Phase 2'    THEN 'FTTH Phase 2'
        WHEN sig ~* '(US Cellular|USCC)'           THEN 'USCC'
        WHEN sig ~* 'Gulf ?Services'               THEN 'Gulf Services'
        ELSE 'EXCLUDED'
    END,
    'rule',
    NULL
FROM (
    SELECT DISTINCT reference.market_signature(asset_identifier) AS sig
    FROM data_raw.raw_assets
    WHERE asset_identifier IS NOT NULL
) s
WHERE sig IS NOT NULL;

UPDATE reference.ref_market_bucket_crosswalk
SET notes = 'Auto-seeded residue; mostly 2023 TSC/Viaero/Nemont legacy outside the rate sheet.'
WHERE market_bucket = 'EXCLUDED';
