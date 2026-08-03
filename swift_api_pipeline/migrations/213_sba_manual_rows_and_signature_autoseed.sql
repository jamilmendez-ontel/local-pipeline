-- 213: SBA manual crosswalk rows + new-signature auto-seed function
-- (1) Manual reclassification (confirmed by Jamil 2026-08-03): paths like
--     'Andrew Tech/VZW/SBA/CAR-TN/NSB Macro/17460177/May 2026' carry an extra SBA
--     (tower company) segment between carrier and market, pushing the 'NSB Macro'
--     scope outside the 3-segment signature window — the rules saw 'VZW/SBA/CAR-TN'
--     and EXCLUDED ~134h of active 2026 NSB Macro work. Both SBA signatures are
--     NSB Macro → 'VZW Embedded / Macro'. First use of source='manual'.
-- (2) reference.seed_new_market_signatures(): the col-67 rule CASE as a callable —
--     inserts any signature observed in raw_assets that the crosswalk doesn't know,
--     classifying by rule (EXCLUDED fallback gets a NEEDS-REVIEW note). Called by
--     the pipeline after each assets extract (transform.py seed_market_signatures),
--     so new markets self-seed and only rule-proof ones need a human.

UPDATE reference.ref_market_bucket_crosswalk
SET market_bucket = 'VZW Embedded / Macro',
    source        = 'manual',
    notes         = 'Extra SBA (tower co) segment between carrier and market pushes the '
                    'NSB Macro scope outside the 3-segment signature window. Paths: '
                    'Andrew Tech/VZW/SBA/<market>/NSB Macro/... Confirmed Jamil 2026-08-03.',
    updated_at    = now()
WHERE market_signature IN ('VZW/SBA/CAR-TN', 'VZW/SBA/GA-AL');

CREATE OR REPLACE FUNCTION reference.seed_new_market_signatures()
RETURNS TABLE(market_signature text, market_bucket text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $$
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
    CASE
        WHEN NOT (sig ~* '(VZW|DONOR|MDU|Ground Scope|Shelter Remodel|BidWalk|Decom|Westell|CGC Remote|AT&T|DISH|TMO|T-Mobile|FTTH|US Cellular|USCC|Gulf ?Services)')
            THEN 'auto-seeded EXCLUDED by pipeline — NEEDS REVIEW'
        ELSE 'auto-seeded by pipeline'
    END
FROM (
    SELECT DISTINCT reference.market_signature(r.asset_identifier) AS sig
    FROM data_raw.raw_assets r
    WHERE r.asset_identifier IS NOT NULL
) s
WHERE sig IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM reference.ref_market_bucket_crosswalk c
      WHERE c.market_signature = s.sig
  )
RETURNING market_signature, market_bucket;
$$;

GRANT EXECUTE ON FUNCTION reference.seed_new_market_signatures() TO service_role;
