-- Migration 247: add market bucket "Viaero" to reference.ref_task_revenue_rates
-- Applied to OntelDB (voqfjfngdpcvevbkikud) via Supabase MCP on 2026-08-27
-- (Supabase migration name: ref_task_revenue_rates_viaero_20260827).
--
-- Viaero bills on the same rate card as VZW Small Cell, so this clones the 29
-- "VZW Small Cell" rows (task, task_name_norm, duration_hrs, amount_usd) row-for-row
-- under market_bucket = 'Viaero'. 242 -> 271 rows, 14 -> 15 market buckets.
-- Requested by Jamil 2026-08-27.
--
-- Idempotent: keyed on UNIQUE (market_bucket, task_name); a re-run inserts nothing.
-- id is GENERATED ALWAYS AS IDENTITY (never supplied). Dependent object
-- reference.vw_task_revenue_rates_with_red_avg is a plain view (no refresh needed).
--
-- NOTE (not done here): reference.market_signature() anchors only on
-- VZW|AT&T|TMO|DISH|FTTH|USCC|Westell|Gulf Services|AAHI, so Viaero asset paths
-- (<GC>/TSC/Viaero/CO-NE - Overlay/LTE/5G/<Mon YYYY>) yield a NULL signature and
-- analytics.mv_timer_revenue prices them as no_market. Wiring Viaero into the
-- crosswalk (anchor + seed rule) is a separate change.

INSERT INTO reference.ref_task_revenue_rates
  (market_bucket, task_name, task_name_norm, duration_hrs, amount_usd, source_file)
SELECT 'Viaero', task_name, task_name_norm, duration_hrs, amount_usd,
       'clone of VZW Small Cell rates (2026-08-27)'
FROM reference.ref_task_revenue_rates
WHERE market_bucket = 'VZW Small Cell'
ON CONFLICT (market_bucket, task_name) DO NOTHING;

-- Verification (run after apply):
-- SELECT count(*) FROM reference.ref_task_revenue_rates;                            -- 271
-- SELECT count(*) FROM reference.ref_task_revenue_rates WHERE market_bucket='Viaero'; -- 29
-- SELECT count(*) FROM reference.ref_task_revenue_rates v
--   JOIN reference.ref_task_revenue_rates s
--     ON s.market_bucket='VZW Small Cell' AND s.task_name=v.task_name
--    AND s.amount_usd=v.amount_usd AND s.duration_hrs IS NOT DISTINCT FROM v.duration_hrs
--  WHERE v.market_bucket='Viaero';                                                   -- 29
