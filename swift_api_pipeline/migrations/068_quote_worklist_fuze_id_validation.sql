-- 068_quote_worklist_fuze_id_validation.sql
-- Quote Automation: make the Fuze ID column structure-validated instead of
-- purely positional. A Fuze ID is a 6-9 digit number (same \d{6,9} pattern the
-- upstream fa_number extraction uses). The old formula grabbed whatever sat in
-- the Fuze position, so non-Fuze tokens (T-Mobile bands like "L600", FTTH
-- "Phase 1", "Anchor", blanks from "//") leaked into the column and were only
-- flagged after the fact.
--
-- New rule (future-proof — rejects anything that isn't Fuze-shaped):
--   fuze_id = the Fuze-position value IF it matches \d{6,9},
--             else the path's FA token (fa_number, also \d{6,9}, recovers the
--             FA from 8+ segment paths the positional parse left blank),
--             else NULL.
-- Verified safe on live data (2026-06-08): of 304 positionally-numeric values
-- all 304 already equalled fa_number (0 disagreements); 3 long-path rows gain a
-- recovered FA; 40 non-numeric junk values become NULL.
--
-- needs_review is unchanged in spirit but now keys off the RAW positional value
-- (fuze_pos_raw) rather than the cleaned output, so it still flags paths that
-- deviate from the standard layout (n NOT IN (5,6,7), or a non-Fuze token in the
-- expected Fuze position) — the OTHER parsed columns stay suspect there even when
-- the FA itself was recovered.

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
    -- raw value in the position the Fuze ID is *expected* (NULL when none expected)
    CASE WHEN n=7 THEN p[6]
         WHEN n=6 AND (p[5] ~ '^[0-9]+$') THEN p[5]
         ELSE NULL END AS fuze_pos_raw
  FROM w
)
SELECT
  task_did, asset_id, asset_name, task_name, task_status, project_did, fa_number,
  segment_count, subcon, gc, carrier, market, project,
  -- structure-validated Fuze ID: valid position -> FA token -> NULL
  CASE WHEN fuze_pos_raw ~ '^\d{6,9}$' THEN fuze_pos_raw
       WHEN fa_number   ~ '^\d{6,9}$' THEN fa_number
       ELSE NULL END AS fuze_id,
  (segment_count NOT IN (5,6,7)
   OR (fuze_pos_raw IS NOT NULL AND fuze_pos_raw !~ '^\d{6,9}$')) AS needs_review
FROM parsed;
