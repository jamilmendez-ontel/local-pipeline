-- 067_quote_worklist_enriched.sql
-- Quote Automation: enrich the QP+PO worklist with the Google Sheet's
-- "QP and PO Issued Tasks" columns (Subcon, GC, Carrier, Market, Project, Fuze ID),
-- replicating the sheet's positional SPLIT-by-"/" formulas 1:1 so output matches
-- what accounting verifies against. Pure positional parse (no org table needed).
--
-- Segment layout by count n:
--   n=7  Subcon / GC / Carrier / Market / Project / FuzeID / Date
--   n=6  (seg5 numeric)      GC / Carrier / Market / Project / FuzeID / Date
--   n=6  (seg5 non-numeric)  Subcon / GC / Carrier / Market / Project / Date
--   n=5  GC / Carrier / Market / Project / Date
--   other (n>=8) -> all blank (matches the sheet)
--
-- needs_review = TRUE when n NOT IN (5,6,7) OR the parsed fuze_id is non-numeric
-- (the latter catches T-Mobile band-suffixed paths where a band like "L600"
-- lands in the Fuze ID slot). "Project" here = the path's scope segment
-- (Embedded / Small Cell / NSB / ...), exactly as the sheet labels it.

CREATE OR REPLACE VIEW analytics.v_quote_worklist_enriched AS
WITH w AS (
  SELECT *,
         string_to_array(asset_id,'/') AS p,
         array_length(string_to_array(asset_id,'/'),1) AS n
  FROM analytics.v_quote_worklist
), parsed AS (
  SELECT
    task_did, asset_id, asset_name, task_name, task_status, project_did, fa_number,
    n AS segment_count,
    CASE WHEN n=7 THEN p[1]
         WHEN n=6 AND NOT (p[5] ~ '^[0-9]+$') THEN p[1]
         ELSE NULL END AS subcon,
    CASE WHEN n=7 THEN p[2]
         WHEN n=6 THEN CASE WHEN p[5] ~ '^[0-9]+$' THEN p[1] ELSE p[2] END
         WHEN n=5 THEN p[1]
         ELSE NULL END AS gc,
    CASE WHEN n=7 THEN p[3]
         WHEN n=6 THEN CASE WHEN p[5] ~ '^[0-9]+$' THEN p[2] ELSE p[3] END
         WHEN n=5 THEN p[2]
         ELSE NULL END AS carrier,
    CASE WHEN n=7 THEN p[4]
         WHEN n=6 THEN CASE WHEN p[5] ~ '^[0-9]+$' THEN p[3] ELSE p[4] END
         WHEN n=5 THEN p[3]
         ELSE NULL END AS market,
    CASE WHEN n=7 THEN p[5]
         WHEN n=6 THEN CASE WHEN p[5] ~ '^[0-9]+$' THEN p[4] ELSE p[5] END
         WHEN n=5 THEN p[4]
         ELSE NULL END AS project,
    CASE WHEN n=7 THEN p[6]
         WHEN n=6 AND (p[5] ~ '^[0-9]+$') THEN p[5]
         ELSE NULL END AS fuze_id
  FROM w
)
SELECT parsed.*,
       (segment_count NOT IN (5,6,7)
        OR (fuze_id IS NOT NULL AND fuze_id !~ '^[0-9]+$')) AS needs_review
FROM parsed;
