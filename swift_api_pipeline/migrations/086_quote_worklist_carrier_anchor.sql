-- 086_quote_worklist_carrier_anchor.sql
-- Quote Automation: replace the rigid POSITIONAL Site-ID parse (067/068) with a
-- CARRIER-ANCHORED parse. The carrier is a closed set {AT&T, T-Mobile, VZW}; we
-- find it in the '/'-path and pivot on it. This handles variable-length paths the
-- positional logic gave up on (n>=8 -> all blank) and fixes misaligned columns.
--
--   left of carrier : GC = segment adjacent to carrier; Subcon = everything before GC
--   right of carrier: Date = last segment (mmm yyyy); Fuze = numeric segment just
--                     before the date; what remains = Market, Project
--
-- "Flag the rest" policy (validated 2026-06-09 on 282 live rows; matches a JS
-- prototype exactly: 40 paths re-parse, 35 carriers corrected, needs_review 11->36):
--   * no carrier found (FTTH/fiber, ~29) -> all category cols NULL + needs_review
--   * last segment isn't a mmm-yyyy date  -> needs_review
--   * 0 market/project segments, or 3+ (band-heavy / irregular) -> needs_review
-- Same output columns/order/types as 067, so mv_quote_review (the only dependent)
-- keeps working; REFRESH it after applying. Column meanings unchanged; "Project"
-- here = the path's scope segment (Embedded / Small Cell / NSB / ...).

CREATE OR REPLACE VIEW analytics.v_quote_worklist_enriched AS
WITH base AS (
  SELECT
    task_did, asset_id, asset_name, task_name, task_status, project_did, fa_number,
    ARRAY(SELECT btrim(x) FROM unnest(string_to_array(asset_id,'/')) AS x WHERE btrim(x) <> '') AS p
  FROM analytics.v_quote_worklist
), a AS (
  SELECT *,
    array_length(p,1) AS n,
    (SELECT min(i) FROM generate_subscripts(p,1) AS i
       WHERE upper(regexp_replace(p[i],'\s+',' ','g'))
             IN ('AT&T','ATT','T-MOBILE','TMO','TMOBILE','VZW','VERIZON')) AS ci
  FROM base
), r AS (
  SELECT *,
    CASE WHEN ci IS NOT NULL THEN p[n] END AS last_seg,
    CASE WHEN ci IS NOT NULL AND n-1 > ci AND p[n-1] ~ '^[0-9]{4,}$' THEN p[n-1] END AS fuze_raw,
    CASE WHEN ci IS NOT NULL AND n-1 > ci AND p[n-1] ~ '^[0-9]{4,}$' THEN n-2 ELSE n-1 END AS mp_end
  FROM a
), parsed AS (
  SELECT
    task_did, asset_id, asset_name, task_name, task_status, project_did, fa_number,
    n AS segment_count,
    CASE WHEN ci >= 3 THEN array_to_string(p[1:ci-2],' / ') END AS subcon,
    CASE WHEN ci >= 2 THEN p[ci-1] END AS gc,
    CASE upper(regexp_replace(p[ci],'\s+',' ','g'))
      WHEN 'AT&T' THEN 'AT&T' WHEN 'ATT' THEN 'AT&T'
      WHEN 'T-MOBILE' THEN 'T-Mobile' WHEN 'TMO' THEN 'T-Mobile' WHEN 'TMOBILE' THEN 'T-Mobile'
      WHEN 'VZW' THEN 'VZW' WHEN 'VERIZON' THEN 'VZW'
    END AS carrier,
    CASE WHEN ci IS NOT NULL AND mp_end >= ci+1 THEN p[ci+1] END AS market,
    CASE WHEN ci IS NULL THEN NULL
         WHEN mp_end = ci+2 THEN p[ci+2]
         WHEN mp_end >= ci+3 THEN array_to_string(p[ci+2:mp_end],' / ')
         ELSE NULL END AS project,
    fuze_raw AS fuze_id,
    ci, last_seg,
    CASE WHEN ci IS NULL THEN NULL ELSE GREATEST(mp_end-ci,0) END AS mp_count
  FROM r
)
SELECT
  task_did, asset_id, asset_name, task_name, task_status, project_did, fa_number,
  segment_count, subcon, gc, carrier, market, project, fuze_id,
  (
    ci IS NULL
    OR last_seg IS NULL
    OR NOT (upper(regexp_replace(last_seg,'\s+',' ','g')) ~ '^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*[0-9]{4}')
    OR mp_count = 0
    OR mp_count >= 3
  ) AS needs_review
FROM parsed;
