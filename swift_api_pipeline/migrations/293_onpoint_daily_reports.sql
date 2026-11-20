-- 293: OnPoint daily reports land in OntelDB (raw payload + typed staging + user dim).
-- APPLIED 2026-11-20 ~4:40 AM ET via MCP apply_migration. Verified: 3 tables, RLS on, service_role only, 3 schema_metadata rows; first load Nov 1-21 = 1,339 reports, 96 users.
--
-- Why: from November 2026 the team files daily reports and clocks in on OnPoint
-- (onpoint.ontel.co) instead of Swift. Swift hour rows fell from ~496/week
-- (Oct 26) to 71 (Nov 16-19); Nov 9-19 had 654 person-days filed ONLY in
-- OnPoint. DRMC reads Swift-shaped tables, so it lost most of November.
-- This migration only creates the landing tables; 294 feeds them into the
-- DRMC views.
--
-- Source: OnPoint v1 API (`time.approvals.list` per report_date and status
-- submitted / approved / rejected; `users.get` for email). Loader:
-- swift_api_pipeline/run_onpoint_daily_reports.py (GHA "Pipeline: OnPoint Daily
-- Reports"). Direct OnPoint DB access is out by OnPoint policy, API only.
-- One OnPoint report = one person-day (unique user_id + report_date), the same
-- grain as a Swift daily report.
--
-- Window semantics: each run re-pulls a trailing window of report dates and
-- DELETES staging rows in that window the API no longer lists (a report sent
-- back to draft or cancelled). Drafts (pending / in_progress) are never landed.
--
-- Rollback:
--   drop table data_staging.stg_onpoint_daily_reports;
--   drop table data_raw.raw_onpoint_daily_reports;
--   drop table data_staging.stg_onpoint_users;
--   delete from agent.schema_metadata where table_name in
--     ('stg_onpoint_daily_reports','raw_onpoint_daily_reports','stg_onpoint_users');

create table data_raw.raw_onpoint_daily_reports (
  report_id   uuid        primary key,
  payload     jsonb       not null,
  fetched_at  timestamptz not null default now()
);

create table data_staging.stg_onpoint_users (
  user_id        uuid        primary key,
  email          text,
  name           text,
  role           text,
  user_type      text,
  manager_id     uuid,
  co_manager_id  uuid,
  suspended_at   timestamptz,
  loaded_at      timestamptz not null default now()
);
create index idx_stg_onpoint_users_email on data_staging.stg_onpoint_users (lower(email));

create table data_staging.stg_onpoint_daily_reports (
  report_id              uuid        primary key,
  user_id                uuid        not null,
  report_date            date        not null,
  status                 text        not null,
  self_reported_hours    numeric,
  worked_minutes         integer,
  computed_timer_hours   numeric,
  time_in                timestamptz,
  time_out               timestamptz,
  submitted_at           timestamptz,
  notes                  text,
  review_comment         text,
  owner_name             text,
  created_at             timestamptz,
  updated_at             timestamptz,
  loaded_at              timestamptz not null default now(),
  unique (user_id, report_date)
);
create index idx_stg_onpoint_dr_date on data_staging.stg_onpoint_daily_reports (report_date);

alter table data_raw.raw_onpoint_daily_reports      enable row level security;
alter table data_staging.stg_onpoint_users          enable row level security;
alter table data_staging.stg_onpoint_daily_reports  enable row level security;

revoke all on data_raw.raw_onpoint_daily_reports, data_staging.stg_onpoint_users,
              data_staging.stg_onpoint_daily_reports from anon, authenticated;
grant all on data_raw.raw_onpoint_daily_reports, data_staging.stg_onpoint_users,
             data_staging.stg_onpoint_daily_reports to service_role;

comment on table data_raw.raw_onpoint_daily_reports is
  'OnPoint daily report payloads as returned by time.approvals.list (filed reports only), upserted on report_id by run_onpoint_daily_reports.py.';
comment on table data_staging.stg_onpoint_users is
  'OnPoint users (users.get): uuid -> email, manager. Join key to reference.ref_employee_emails / ref_employees.';
comment on table data_staging.stg_onpoint_daily_reports is
  'OnPoint daily reports, one row per person-day (submitted / approved / rejected only). Replaces Swift daily reports from 2026-11. Rows in the loader''s re-pulled window that OnPoint no longer lists are deleted.';

insert into agent.schema_metadata (schema_name, table_name, column_name, description)
select s, t, null, d from (values
  ('data_raw', 'raw_onpoint_daily_reports', 'OnPoint daily report payloads (time.approvals.list), one per report_id.'),
  ('data_staging', 'stg_onpoint_users', 'OnPoint users: uuid, email, manager. Join to the roster by email.'),
  ('data_staging', 'stg_onpoint_daily_reports', 'OnPoint daily reports per person-day (filed only): status, self-reported hours, time in/out, submitted_at, notes. Source for DRMC from 2026-11.')
) v(s, t, d)
where not exists (select 1 from agent.schema_metadata m
                  where m.schema_name = v.s and m.table_name = v.t and m.column_name is null);
