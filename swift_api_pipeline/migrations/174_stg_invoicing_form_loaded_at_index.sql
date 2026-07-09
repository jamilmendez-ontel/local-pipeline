-- 174: index stg_invoicing_form(loaded_at). PostgREST freshness probes
-- (ORDER BY loaded_at LIMIT 1) had no index to serve them, so every call
-- seq-scanned and sorted the whole 312 MB table: 3,110 calls / 545 GB of
-- cumulative disk reads by 2026-07-09, a contributor to the Supabase Disk
-- IO budget depletion alerts. A plain btree serves both min and max probes.

create index if not exists idx_stg_invoicing_form_loaded_at
  on data_staging.stg_invoicing_form (loaded_at);
