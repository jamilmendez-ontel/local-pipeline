-- 176: shadow tables for the INCREMENTAL asset-tasks pipeline (TS17-19 pilot).
-- Runs in parallel with the full-reload pipeline; nothing here is read or
-- written by the current pipeline. Loading contract: guarded upserts by
-- natural key + keep-list reconcile inside successfully-fetched scopes;
-- NO run_id sweeps. stg_asset_tasks_inc mirrors stg_asset_tasks business
-- columns so the drift audit can diff them directly. Cutover or teardown
-- is an explicit later decision. Applied to prod 2026-07-09 via MCP.
-- Plan: docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md

create table data_raw.raw_asset_tasks_inc (
  task_did     text primary key,
  asset_did    text not null,
  project_did  text not null,
  data         jsonb not null,
  last_updated timestamptz,
  loaded_at    timestamptz not null default now()
);
create index idx_raw_asset_tasks_inc_project on data_raw.raw_asset_tasks_inc (project_did);
alter table data_raw.raw_asset_tasks_inc enable row level security;

create table data_staging.stg_assets_inc (
  asset_did              text primary key,
  project_did            text not null,
  asset_id               text,
  asset_name             text,
  asset_requirement_count integer,
  last_updated           timestamptz,
  loaded_at              timestamptz not null default now()
);
create index idx_stg_assets_inc_project on data_staging.stg_assets_inc (project_did);
alter table data_staging.stg_assets_inc enable row level security;

create table data_staging.stg_asset_tasks_inc (
  task_did                    text primary key,
  project_did                 text not null,
  project_status              text,
  asset_did                   text not null,
  asset_id                    text,
  asset_name                  text,
  asset_requirement_count     integer,
  task_name                   text,
  task_status                 text,
  task_scheduled              date,
  task_assigned_to_did        text,
  task_assigned_to_collection text,
  task_assigned_to_name       text,
  task_assigned_to_email      text,
  task_submitted_on           date,
  task_submitted_by_did       text,
  task_submitted_by_name      text,
  task_submitted_by_email     text,
  task_approved_on            date,
  task_approved_by_did        text,
  task_approved_by_name       text,
  task_approved_by_email      text,
  task_cancelled_on           date,
  task_cancelled_by_did       text,
  task_cancelled_by_name      text,
  task_cancelled_by_email     text,
  task_name_clean             text,
  last_updated                timestamptz,
  loaded_at                   timestamptz not null default now()
);
create index idx_stg_asset_tasks_inc_project on data_staging.stg_asset_tasks_inc (project_did);
create index idx_stg_asset_tasks_inc_asset on data_staging.stg_asset_tasks_inc (asset_did);
alter table data_staging.stg_asset_tasks_inc enable row level security;

-- Test-week evidence trail: one row per project per audit run. The pilot
-- decision (expand / fix / abort) is made from this table's week of data.
create table pipeline.inc_audit_results (
  id             bigint generated always as identity primary key,
  audited_at     timestamptz not null default now(),
  project_did    text not null,
  rows_current   integer,
  rows_inc       integer,
  hash_match     boolean,
  missing_in_inc text[],   -- task_dids in current but not in _inc (LIMIT 50)
  extra_in_inc   text[],   -- task_dids in _inc but not in current (LIMIT 50)
  column_diffs   jsonb,    -- {task_did: {col: [current, inc]}} sample (LIMIT 20)
  notes          text
);
alter table pipeline.inc_audit_results enable row level security;
