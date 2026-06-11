-- 096_quote_norm_collapse_slash.sql
-- Applied live 2026-06-11 via Supabase MCP apply_migration.
--
-- BUG: A Quote Directory entry whose gc/carrier/market/project contains a "/"
-- never matched its task in analytics.v_quote_review, so the assigned recipient
-- never surfaced in the Queue (the "save to directory does nothing" symptom).
--
-- ROOT CAUSE: the directory match in v_quote_review builds a per-task `pathstr`
-- that collapses any run of  [/_\s\-]  to a single space, then requires each
-- normalized directory part (gc_n/carrier_n/market_n/project_n from
-- analytics.v_quote_directory_keys) to appear as a token in it. Those parts are
-- normalized by analytics.quote_norm(), whose character class was [_\-]+ — it
-- collapsed underscores and dashes but NOT "/". So a part like "Miami / Anchor"
-- normalized to "MIAMI / ANCHOR" while the path held "MIAMI ANCHOR", and the
-- POSITION() token test returned 0. Dash/space parts (e.g. "FL - Excalibur",
-- "Capacity-Sector-Add") matched fine, which is why the bug looked intermittent.
--
-- FIX: add "/" to quote_norm's class ([_\-]+ -> [/_\-]+) so the directory-key
-- side normalizes "/" exactly like the pathstr side. quote_norm is referenced
-- only by v_quote_directory_keys (checked pg_depend), which is a plain view, so
-- this takes effect immediately with no MV refresh.
--
-- Preflight (read-only) before applying:
--   * Only 3 directory rows contain "/": ids 201/202 (MasTec FTTH, carrier NULL —
--     still excluded by the carrier-required predicate) and id 216 (the repro).
--   * Re-normalizing with "/" collapsed introduces ZERO new recipient conflicts
--     (no 4-tuple collapsed two distinct recipient/cc sets).
-- Post-apply: task 6FB1067B (TSC / T-Mobile / FL - Excalibur / Miami / Anchor)
--   flipped directory_matched false -> true; STS control row unchanged.

CREATE OR REPLACE FUNCTION analytics.quote_norm(t text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
AS $function$
  SELECT btrim(regexp_replace(upper(regexp_replace(coalesce(t,''),'[/_\-]+',' ','g')),'\s+',' ','g'))
$function$;
