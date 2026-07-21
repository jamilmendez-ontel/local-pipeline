-- 186: loaded_at indexes for the remaining freshness-probe tables. The
-- monitoring probe (7-way UNION ALL of MAX(loaded_at), 12 calls/day) reads
-- ~708 MB per call because four of its tables still resolve MAX() with a
-- seq scan; flagged in the 2026-07-20 Disk IO Budget depletion follow-up.
-- stg_asset_tasks already has idx_stg_asset_tasks_loaded_at (index-only
-- scan confirmed); stg_user_priorities (18 MB, rewritten 288x/day) and
-- stg_projects (1 MB) are deliberately skipped — index maintenance there
-- would cost more than the probe reads. Same pattern as migration 174
-- (stg_invoicing_form).

create index if not exists idx_stg_qa_form_loaded_at
  on data_staging.stg_qa_form (loaded_at);

create index if not exists idx_stg_timer_activities_loaded_at
  on data_staging.stg_timer_activities (loaded_at);

create index if not exists idx_stg_timer_activities_clean_loaded_at
  on data_staging.stg_timer_activities_clean (loaded_at);

create index if not exists idx_stg_assets_loaded_at
  on data_staging.stg_assets (loaded_at);
