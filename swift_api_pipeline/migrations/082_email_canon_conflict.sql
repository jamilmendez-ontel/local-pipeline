-- 082_email_canon_conflict.sql
-- Make directory conflict detection immune to email order / whitespace / case.
-- analytics.email_canon(): split a comma list, trim+lowercase each, drop blanks,
-- dedupe, sort -> canonical string. v_quote_directory_keys now flags has_conflict
-- by comparing CANONICAL recipient/cc, so 'a,b' vs 'b,a ' is NOT a conflict.
-- (Verified 2026-06-09: the 6 existing conflicts are genuinely different email
-- SETS, not formatting; this only prevents FUTURE false conflicts.)

CREATE OR REPLACE FUNCTION analytics.email_canon(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(string_agg(e, ',' ORDER BY e), '')
  FROM (SELECT DISTINCT lower(btrim(x)) AS e
        FROM unnest(string_to_array(coalesce(t,''), ',')) x
        WHERE btrim(x) <> '') z
$$;

CREATE OR REPLACE VIEW analytics.v_quote_directory_keys AS
SELECT
  analytics.quote_norm(gc)      AS gc_n,
  analytics.quote_norm(carrier) AS carrier_n,
  analytics.quote_norm(market)  AS market_n,
  analytics.quote_norm(project) AS project_n,
  (count(DISTINCT analytics.email_canon(recipient)||'~~'||analytics.email_canon(cc)) > 1) AS has_conflict,
  CASE WHEN count(DISTINCT analytics.email_canon(recipient)||'~~'||analytics.email_canon(cc)) > 1 THEN NULL ELSE min(recipient) END AS recipient,
  CASE WHEN count(DISTINCT analytics.email_canon(recipient)||'~~'||analytics.email_canon(cc)) > 1 THEN NULL ELSE min(cc) END AS cc
FROM reference.ref_quote_directory
GROUP BY 1,2,3,4;
GRANT SELECT ON analytics.v_quote_directory_keys TO anon, authenticated, service_role;
