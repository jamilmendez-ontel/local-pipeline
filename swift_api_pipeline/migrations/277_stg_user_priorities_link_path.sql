-- =============================================================================
-- 277_stg_user_priorities_link_path.sql
-- stg_user_priorities.link_path: the Swift web link to the task
-- (https://swiftprojects.io/#/app/assets/tasks/<task_did>/requirements), the
-- same URL pattern the DRMC and Timer emails already build by hand.
--
-- The user-priorities report has no link field (keys verified 2026-10-08), so
-- the column is derived from task_did: transform.py USER_PRIORITY_COLUMNS
-- writes it on every merge from here on, and this migration backfills the
-- rows already in the table. Plain column (not GENERATED) because
-- migrate_data_to_cloud.py copies every column via COPY.
--
-- Also appended (last position, so CREATE OR REPLACE is allowed) to the two
-- views that read the table: analytics.v_user_priorities and
-- analytics.v_user_priorities_effective (the one apps should read; it keeps
-- its explicit column order from migration 237). No view depends on either
-- (pg_depend checked 2026-10-08).
-- =============================================================================

ALTER TABLE data_staging.stg_user_priorities
  ADD COLUMN IF NOT EXISTS link_path TEXT;

UPDATE data_staging.stg_user_priorities
   SET link_path = 'https://swiftprojects.io/#/app/assets/tasks/' || task_did || '/requirements'
 WHERE task_did IS NOT NULL
   AND link_path IS DISTINCT FROM 'https://swiftprojects.io/#/app/assets/tasks/' || task_did || '/requirements';

COMMENT ON COLUMN data_staging.stg_user_priorities.link_path IS
  'Swift web link to the task (requirements tab), derived from task_did.';

CREATE OR REPLACE VIEW analytics.v_user_priorities AS
SELECT up.task_did,
       up.task_name_clean,
       up.status,
       up.milestone,
       up.calendar_status,
       up.assigned_to,
       up.scheduled,
       up.scheduled_by,
       up.display_date,
       up.duration,
       up.pin_type,
       up.submitted_by,
       up.submitted_on,
       up.approved_by,
       up.approved_on,
       up.rejected_by,
       up.rejected_on,
       up.cancelled_by,
       up.cancelled_on,
       up.asset_did,
       a.asset_id,
       a.asset_name,
       up.project_did,
       p.project_name,
       o.org_name,
       up.link_path
  FROM data_staging.stg_user_priorities up
  LEFT JOIN data_staging.stg_assets a ON up.asset_did = a.asset_did
  LEFT JOIN data_staging.stg_projects p ON up.project_did = p.project_did
  LEFT JOIN data_staging.stg_organizations o ON up.org_did = o.org_did;

CREATE OR REPLACE VIEW analytics.v_user_priorities_effective AS
SELECT p.id,
       p.task_did,
       p.asset_did,
       p.org_did,
       p.project_did,
       p.task_name,
       p.milestone,
       p.status,
       p.calendar_status,
       p.assigned_to,
       p.scheduled,
       p.scheduled_by,
       p.display_date,
       p.duration,
       p.pin_type,
       p.submitted_by,
       p.submitted_on,
       p.approved_by,
       p.approved_on,
       p.rejected_by,
       p.rejected_on,
       p.cancelled_by,
       p.cancelled_on,
       p.organization,
       p.project,
       p.asset_id,
       p.asset_name,
       p.run_id,
       p.loaded_at,
       p.task_name_clean,
       a.class                          AS schedule_anomaly_class,
       a.feed_scheduled                 AS schedule_feed_value,
       (a.task_did IS NOT NULL)         AS schedule_anomaly_open,
       (a.task_did IS NOT NULL
        AND p.scheduled IS DISTINCT FROM a.stored_scheduled)
                                        AS schedule_anomaly_stale,
       CASE
           WHEN a.task_did IS NULL THEN p.scheduled
           WHEN p.scheduled IS DISTINCT FROM a.stored_scheduled THEN p.scheduled
           WHEN a.class = 'ghost_schedule' THEN NULL
           WHEN a.class = 'timed_mismatch' AND a.feed_scheduled IS NOT NULL
               THEN a.feed_scheduled
           ELSE p.scheduled
       END                              AS scheduled_effective,
       p.link_path
  FROM data_staging.stg_user_priorities p
  LEFT JOIN pipeline.schedule_audit_anomalies a
         ON a.task_did = p.task_did AND a.status = 'open';

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
  ('data_staging', 'stg_user_priorities', 'link_path',
   'Swift web link to the task (requirements tab)',
   'TEXT. https://swiftprojects.io/#/app/assets/tasks/<task_did>/requirements, derived from task_did. User may say: "link", "Swift link", "open the task".'),
  ('analytics', 'v_user_priorities', 'link_path',
   'Swift web link to the task (requirements tab)',
   'TEXT. Sourced from stg_user_priorities.link_path.'),
  ('analytics', 'v_user_priorities_effective', 'link_path',
   'Swift web link to the task (requirements tab)',
   'TEXT. Sourced from stg_user_priorities.link_path.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
   SET description = EXCLUDED.description, business_context = EXCLUDED.business_context;
