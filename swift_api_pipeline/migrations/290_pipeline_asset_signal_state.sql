-- 290: last-seen Swift asset change-signal values, one row per asset.
-- Purpose (ADR 0007 in ontel-data-platform): the Cloud Run asset-task walker
-- reads each asset's Firebase RTDB change signal (keys asset-tasks, data,
-- timer-activities, each epoch-ms) and compares it with the values stored here
-- to decide which assets' tasks changed and need re-reading.
--   asset_tasks_ms / data_ms / timer_ms : last-seen signal values
--   seen_at                              : when the row was last written
--   full_visited_at                      : last time the asset was fully read
--
-- Force a re-read of a project: delete its rows,
--   delete from pipeline.asset_signal_state where project_did = '...';
-- The next walk then treats every asset in that project as first sight and the
-- rolling walk covers it.
--
-- Rollback: drop table pipeline.asset_signal_state;

create table pipeline.asset_signal_state (
  asset_did        text        primary key,
  project_did      text        not null,
  asset_tasks_ms   bigint,
  data_ms          bigint,
  timer_ms         bigint,
  seen_at          timestamptz not null default now(),
  full_visited_at  timestamptz
);

create index idx_asset_signal_state_project on pipeline.asset_signal_state (project_did);

alter table pipeline.asset_signal_state enable row level security;

-- Writer role: service_role (the only non-default writer on data_staging.stg_assets_inc).
grant all on pipeline.asset_signal_state to service_role;

comment on table pipeline.asset_signal_state is
  'Last-seen Firebase RTDB change-signal values (asset-tasks, data, timer-activities; epoch ms) per Swift asset, used by the asset-signal walk to find changed assets (ADR 0007). Delete a project_did''s rows to force a full re-read.';
