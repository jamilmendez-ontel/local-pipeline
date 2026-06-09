-- 079_quote_directory_normalize.sql
-- Canonical normalization + directory dedup, so format variants connect the same
-- (MS_Embedded == MS Embedded) and the directory stays clean.
--
-- 1) analytics.quote_norm(): upper, unify _ and - to space, collapse whitespace, trim.
-- 2) Merge SAFE duplicate directory rows (same normalized GC/Carrier/Market/Project
--    AND identical recipient/cc) — keep lowest id. (Ran once: 210 -> 207.)
-- 3) analytics.v_quote_directory_keys: one row per normalized key, with recipient/cc
--    when unambiguous and has_conflict=TRUE when the same key has differing recipients
--    (those 6 keys must be resolved by accounting; recipient/cc are NULL for them).

CREATE OR REPLACE FUNCTION analytics.quote_norm(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT btrim(regexp_replace(upper(regexp_replace(coalesce(t,''),'[_\-]+',' ','g')),'\s+',' ','g'))
$$;

WITH d AS (
  SELECT a.id
  FROM reference.ref_quote_directory a
  JOIN reference.ref_quote_directory b
    ON analytics.quote_norm(a.gc)=analytics.quote_norm(b.gc)
   AND analytics.quote_norm(a.carrier)=analytics.quote_norm(b.carrier)
   AND analytics.quote_norm(a.market)=analytics.quote_norm(b.market)
   AND analytics.quote_norm(a.project)=analytics.quote_norm(b.project)
   AND coalesce(a.recipient,'')=coalesce(b.recipient,'')
   AND coalesce(a.cc,'')=coalesce(b.cc,'')
   AND a.id > b.id
)
DELETE FROM reference.ref_quote_directory WHERE id IN (SELECT id FROM d);

CREATE OR REPLACE VIEW analytics.v_quote_directory_keys AS
SELECT
  analytics.quote_norm(gc)      AS gc_n,
  analytics.quote_norm(carrier) AS carrier_n,
  analytics.quote_norm(market)  AS market_n,
  analytics.quote_norm(project) AS project_n,
  (count(DISTINCT coalesce(recipient,'')||'~~'||coalesce(cc,'')) > 1) AS has_conflict,
  CASE WHEN count(DISTINCT coalesce(recipient,'')||'~~'||coalesce(cc,'')) > 1 THEN NULL ELSE min(recipient) END AS recipient,
  CASE WHEN count(DISTINCT coalesce(recipient,'')||'~~'||coalesce(cc,'')) > 1 THEN NULL ELSE min(cc) END AS cc
FROM reference.ref_quote_directory
GROUP BY 1,2,3,4;
GRANT SELECT ON analytics.v_quote_directory_keys TO anon, authenticated, service_role;
