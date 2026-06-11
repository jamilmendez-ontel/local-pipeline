-- 094_quote_worklist_ftth_no_carrier_parse.sql
-- Applied live 2026-06-10 via Supabase MCP apply_migration.
--
-- Extends analytics.v_quote_worklist_enriched (the carrier-anchored parse from 086)
-- with a NO-CARRIER branch for FTTH Site-ID paths, which previously parsed to
-- all-NULL + needs_review.
--
-- No-carrier path shape (carrier ∉ {AT&T,T-Mobile,VZW}):
--   [Subcon /] GC / MarketA / MarketB / FTTH / Phase N [/ Mmm yyyy]
--   anchored on the "FTTH" token:
--     Project = "FTTH Phase N"  (FTTH segment .. last non-date segment, space-joined)
--     Market  = the two segments before FTTH        (e.g. "FL / MO9&10")
--     GC      = the segment before the market        (e.g. "MasTec")
--     Subcon  = anything before GC                   (e.g. "GY Underground")
--     Carrier = NULL (FTTH genuinely has none)
--     date    = trailing "Mmm yyyy" dropped
--
-- Carrier-present rows (ci NOT NULL) keep their EXACT prior logic — validated
-- read-only before apply: 0 of 234 carrier rows changed.
-- Effect: 36 FTTH rows categorized; worklist needs_review 43 -> 7.
-- FTTH stays directory-unmatched (recipient match requires a carrier) — a
-- separate follow-up would add carrier-optional directory entries.
--
-- Only dependent object: analytics.mv_quote_review (refresh after applying).

CREATE OR REPLACE VIEW analytics.v_quote_worklist_enriched AS
WITH base AS (
  SELECT v_quote_worklist.task_did,
         v_quote_worklist.asset_id,
         v_quote_worklist.asset_name,
         v_quote_worklist.task_name,
         v_quote_worklist.task_status,
         v_quote_worklist.project_did,
         v_quote_worklist.fa_number,
         ARRAY( SELECT btrim(x.x) AS btrim
                FROM unnest(string_to_array(v_quote_worklist.asset_id, '/'::text)) x(x)
                WHERE btrim(x.x) <> ''::text) AS p
  FROM analytics.v_quote_worklist
), a AS (
  SELECT base.task_did, base.asset_id, base.asset_name, base.task_name, base.task_status,
         base.project_did, base.fa_number, base.p,
         array_length(base.p, 1) AS n,
         ( SELECT min(i.i) AS min
             FROM generate_subscripts(base.p, 1) i(i)
            WHERE upper(regexp_replace(base.p[i.i], '\s+'::text, ' '::text, 'g'::text)) = ANY (ARRAY['AT&T'::text, 'ATT'::text, 'T-MOBILE'::text, 'TMO'::text, 'TMOBILE'::text, 'VZW'::text, 'VERIZON'::text])) AS ci,
         ( SELECT min(i.i) AS min
             FROM generate_subscripts(base.p, 1) i(i)
            WHERE upper(regexp_replace(base.p[i.i], '\s+'::text, ' '::text, 'g'::text)) = 'FTTH'::text) AS fi
  FROM base
), r AS (
  SELECT a.task_did, a.asset_id, a.asset_name, a.task_name, a.task_status, a.project_did,
         a.fa_number, a.p, a.n, a.ci, a.fi,
         CASE WHEN a.ci IS NOT NULL THEN a.p[a.n] ELSE NULL::text END AS last_seg,
         CASE WHEN a.ci IS NOT NULL AND (a.n - 1) > a.ci AND a.p[a.n - 1] ~ '^[0-9]{4,}$'::text THEN a.p[a.n - 1] ELSE NULL::text END AS fuze_raw,
         CASE WHEN a.ci IS NOT NULL AND (a.n - 1) > a.ci AND a.p[a.n - 1] ~ '^[0-9]{4,}$'::text THEN a.n - 2 ELSE a.n - 1 END AS mp_end,
         CASE WHEN upper(regexp_replace(a.p[a.n], '\s+'::text, ' '::text, 'g'::text)) ~ '^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*[0-9]{4}'::text THEN a.n - 1 ELSE a.n END AS nc_proj_end
  FROM a
), parsed AS (
  SELECT r.task_did, r.asset_id, r.asset_name, r.task_name, r.task_status, r.project_did,
         r.fa_number, r.n AS segment_count,
         CASE
           WHEN r.ci >= 3 THEN array_to_string(r.p[1:r.ci - 2], ' / '::text)
           WHEN r.ci IS NULL AND r.fi IS NOT NULL AND (r.fi - 4) >= 1 THEN array_to_string(r.p[1:r.fi - 4], ' / '::text)
           ELSE NULL::text
         END AS subcon,
         CASE
           WHEN r.ci >= 2 THEN r.p[r.ci - 1]
           WHEN r.ci IS NULL AND r.fi IS NOT NULL AND r.fi >= 4 THEN r.p[r.fi - 3]
           ELSE NULL::text
         END AS gc,
         CASE upper(regexp_replace(r.p[r.ci], '\s+'::text, ' '::text, 'g'::text))
           WHEN 'AT&T'::text THEN 'AT&T'::text
           WHEN 'ATT'::text THEN 'AT&T'::text
           WHEN 'T-MOBILE'::text THEN 'T-Mobile'::text
           WHEN 'TMO'::text THEN 'T-Mobile'::text
           WHEN 'TMOBILE'::text THEN 'T-Mobile'::text
           WHEN 'VZW'::text THEN 'VZW'::text
           WHEN 'VERIZON'::text THEN 'VZW'::text
           ELSE NULL::text
         END AS carrier,
         CASE
           WHEN r.ci IS NOT NULL AND r.mp_end >= (r.ci + 1) THEN r.p[r.ci + 1]
           WHEN r.ci IS NULL AND r.fi IS NOT NULL AND r.fi >= 3 THEN array_to_string(r.p[GREATEST(r.fi - 2, 1):r.fi - 1], ' / '::text)
           ELSE NULL::text
         END AS market,
         CASE
           WHEN r.ci IS NULL AND r.fi IS NOT NULL AND r.nc_proj_end >= r.fi THEN array_to_string(r.p[r.fi:r.nc_proj_end], ' '::text)
           WHEN r.ci IS NULL THEN NULL::text
           WHEN r.mp_end = (r.ci + 2) THEN r.p[r.ci + 2]
           WHEN r.mp_end >= (r.ci + 3) THEN array_to_string(r.p[r.ci + 2:r.mp_end], ' / '::text)
           ELSE NULL::text
         END AS project,
         r.fuze_raw AS fuze_id,
         r.ci, r.fi, r.last_seg, r.nc_proj_end,
         CASE WHEN r.ci IS NULL THEN NULL::integer ELSE GREATEST(r.mp_end - r.ci, 0) END AS mp_count
  FROM r
)
SELECT parsed.task_did,
       parsed.asset_id,
       parsed.asset_name,
       parsed.task_name,
       parsed.task_status,
       parsed.project_did,
       parsed.fa_number,
       parsed.segment_count,
       parsed.subcon,
       parsed.gc,
       parsed.carrier,
       parsed.market,
       parsed.project,
       parsed.fuze_id,
       CASE
         WHEN parsed.ci IS NOT NULL THEN
           (parsed.last_seg IS NULL
            OR NOT upper(regexp_replace(parsed.last_seg, '\s+'::text, ' '::text, 'g'::text)) ~ '^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC).*[0-9]{4}'::text
            OR parsed.mp_count = 0 OR parsed.mp_count >= 3)
         WHEN parsed.fi IS NOT NULL AND parsed.fi >= 4 AND parsed.nc_proj_end >= parsed.fi + 1 THEN false
         ELSE true
       END AS needs_review
FROM parsed;

-- Refresh the only dependent object so the app sees the new parse:
-- SELECT analytics.refresh_one_mv('mv_quote_review');
