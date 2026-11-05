-- 290: last-seen Swift asset change-signal values, one row per asset.
-- APPLIED 2026-11-05 13:16 ET via MCP apply_migration. Verified: table + PK(asset_did) + idx_asset_signal_state_project, RLS on, owner postgres, service_role=arwdDxt, anon no SELECT, agent.schema_metadata 1 row, 0 rows.
-- Purpose (ADR 0007 in ontel-data-platform): the Cloud Run asset-task walker
-- reads each asset's Firebase RTDB change signal (keys asset-tasks, data,
-- timer-activities, each epoch-ms) and compares it with the values stored here
-- to decide which assets' tasks changed and need re-reading.
--   asset_tasks_ms / data_ms / timer_ms : last-seen signal values
--   seen_at                              : when the row was last written
--   full_visited_at                      : last time the asset was fully read
--
-- Force a re-read of a project: mark every asset as moved,
--   update pipeline.asset_signal_state set asset_tasks_ms = -1 where project_did = '...';
-- The next signal walk re-reads them all (capped per walk, newest first).
-- Deleting a project's rows does NOT re-read anything: every asset becomes a
-- first sight (values saved, tasks not re-read), the rolling walk covers them
-- within 12 h, and the project gets no rolling sweep row for about 12 h
-- (its full_visited_at stamps are gone).
--
-- Rollback: drop table pipeline.asset_signal_state;
--           delete from agent.schema_metadata where schema_name = 'pipeline' and table_name = 'asset_signal_state';

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
  'Last-seen Firebase RTDB change-signal values (asset-tasks, data, timer-activities; epoch ms) per Swift asset, used by the asset-signal walk to find changed assets (ADR 0007). To force a re-read of a project set its asset_tasks_ms to -1; deleting a project_did''s rows only makes every asset a first sight (no re-read).';

-- Catalog row (DATABASE_ARCHITECTURE.md section 5). NULL column_name never hits the unique key, so guard with not exists.
insert into agent.schema_metadata (schema_name, table_name, column_name, description)
select 'pipeline', 'asset_signal_state', null,
       'Last-seen RTDB change-signal values per Swift asset (ADR 0007); written by the asset-signal walk.'
where not exists (select 1 from agent.schema_metadata
                  where schema_name = 'pipeline' and table_name = 'asset_signal_state' and column_name is null);
