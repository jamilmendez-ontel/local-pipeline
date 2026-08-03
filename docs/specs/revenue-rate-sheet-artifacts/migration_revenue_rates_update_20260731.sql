-- Migration: ref_task_revenue_rates_update_20260731
-- Applied to OntelDB (voqfjfngdpcvevbkikud) via Supabase MCP on 2026-07-31 (07:54 ET).
-- Source: "Revenue Metrics_Duration & Amount_20260731 (1).xlsx"  (242 rows, 14 market buckets)
-- Prior load: "Revenue Metrics_Duration & Amount (1).xlsx"       (239 rows) -- see migration_revenue_rates.sql
--
-- Diff vs prior load: 3 added, 2 changed, 0 removed  (239 -> 242 rows).
--   ADDED:
--     Ground Scope         | Live Review Complete | 1  hr | $40
--     VZW Embedded / Macro | 360 Tour Complete    | NULL  | $100
--     VZW Small Cell       | 360 Tour Complete    | NULL  | $100
--   CHANGED:
--     Ground Scope | Data Pre-Fill Complete | 0.2/$28 -> 0/$0
--     Ground Scope | Final COP Complete      | 1/$100  -> 1/$128
--
-- The two "360 Tour Complete" rows carry an amount but no stated duration in the sheet,
-- so duration_hrs was made nullable (DROP NOT NULL) and those rows store NULL (distinct
-- from the ~48 intentional 0-hour revision rows).
--
-- Upsert keyed on the UNIQUE (market_bucket, task_name) constraint; id is GENERATED ALWAYS
-- AS IDENTITY so existing ids / created_at are preserved. Dependent object
-- reference.vw_task_revenue_rates_with_red_avg is a plain view (no refresh needed).

ALTER TABLE reference.ref_task_revenue_rates
  ALTER COLUMN duration_hrs DROP NOT NULL;

INSERT INTO reference.ref_task_revenue_rates
  (market_bucket, task_name, task_name_norm, duration_hrs, amount_usd, source_file)
VALUES
  -- added
  ('Ground Scope',         'Live Review Complete', 'Live Review Complete', 1,    40,  'Revenue Metrics_Duration & Amount_20260731 (1).xlsx'),
  ('VZW Embedded / Macro', '360 Tour Complete',    '360 Tour Complete',    NULL, 100, 'Revenue Metrics_Duration & Amount_20260731 (1).xlsx'),
  ('VZW Small Cell',       '360 Tour Complete',    '360 Tour Complete',    NULL, 100, 'Revenue Metrics_Duration & Amount_20260731 (1).xlsx'),
  -- changed
  ('Ground Scope',         'Data Pre-Fill Complete', 'Data Pre-Fill Complete', 0, 0,   'Revenue Metrics_Duration & Amount_20260731 (1).xlsx'),
  ('Ground Scope',         'Final COP Complete',     'Final COP Complete',     1, 128, 'Revenue Metrics_Duration & Amount_20260731 (1).xlsx')
ON CONFLICT (market_bucket, task_name) DO UPDATE SET
  duration_hrs = EXCLUDED.duration_hrs,
  amount_usd   = EXCLUDED.amount_usd,
  source_file  = EXCLUDED.source_file,
  updated_at   = now();
