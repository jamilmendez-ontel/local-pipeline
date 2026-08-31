-- 248: wire the Viaero market into the crosswalk (companion to 247, which added the
-- Viaero rate rows). Applied to OntelDB via Supabase MCP 2026-08-27 (name 248_market_crosswalk_viaero).
-- Viaero asset paths look like
--   '<GC>/TSC/Viaero/CO-NE - Overlay/LTE/5G/<Mon YYYY>'
-- and carry none of the carrier anchors market_signature() knew, so all 309 Viaero
-- assets produced a NULL signature, never reached the crosswalk, and every Viaero
-- timer priced as no_market in analytics.mv_timer_revenue. Requested by Jamil 2026-08-27.
-- (1) market_signature(): add Viaero to the anchor regex (3-segment window applies:
--     'Viaero/CO-NE - Overlay/LTE'). No existing signature changes: 0 of 309 Viaero
--     paths had a signature before this, and paths with an earlier anchor keep it.
-- (2) seed_new_market_signatures(): add the 'Viaero' rule + anchor to the review list.
-- (3) seed now so the rows exist before the next assets extract.
-- Seeded 5 signatures: Viaero/CO/LTE, Viaero/CO-NE/LTE, Viaero/CO-NE - Overlay/LTE,
-- Viaero/CO-NE/UMTS, Viaero/NE/LTE (all rule -> Viaero; 309/309 assets classify).
-- After apply: refresh_one_mv('mv_timer_revenue') THEN refresh_one_mv('mv_timer_revenue_daily').

CREATE OR REPLACE FUNCTION reference.market_signature(p_path text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
 SET search_path TO ''
AS $function$
WITH segs AS (
    SELECT seg, pos
    FROM unnest(string_to_array(p_path, '/')) WITH ORDINALITY AS t(seg, pos)
),
anchor AS (
    SELECT min(pos) AS cpos
    FROM segs
    WHERE seg ~* '^(VZW|AT&T|TMO|T-Mobile|DISH|FTTH|USCC|US Cellular|Westell|Gulf ?Services|AAHI|Viaero)'
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
$function$;

CREATE OR REPLACE FUNCTION reference.seed_new_market_signatures()
 RETURNS TABLE(market_signature text, market_bucket text)
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
INSERT INTO reference.ref_market_bucket_crosswalk (market_signature, market_bucket, source, notes)
SELECT sig,
    CASE
        WHEN sig ~* 'VZW' AND sig ~* '(Embedded|Macro|NSB)'
             AND sig !~* 'Small Cell' AND sig !~* 'Decom'
             AND sig !~* '(AAHI|DONOR|MDU)' AND sig !~* 'Ground Scope'
                                                   THEN 'VZW Embedded / Macro'
        WHEN sig ~* 'VZW' AND sig ~* 'Small Cell'  THEN 'VZW Small Cell'
        WHEN sig ~* '^Viaero'                      THEN 'Viaero'
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
        WHEN NOT (sig ~* '(VZW|DONOR|MDU|Ground Scope|Shelter Remodel|BidWalk|Decom|Westell|CGC Remote|AT&T|DISH|TMO|T-Mobile|FTTH|US Cellular|USCC|Gulf ?Services|Viaero)')
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
$function$;

-- (3) seed the Viaero signatures now (any other unseen signature seeds by rule too)
SELECT * FROM reference.seed_new_market_signatures();
