-- =============================================================================
-- 272_gc_tracker_task_requirements_view.sql
-- GC tracker: ONE combined task + requirement view for the team (Jamil, 2026-09-25).
-- One row per requirement, with its parent task's columns repeated; tasks that have
-- no requirement rows still appear once with the requirement columns NULL, so the view
-- also lists every tracker item. Column set = Jamil's pick in
-- ontel-data-platform/reference/gc-tracker/tracker-task-requirements-2026-09-25.xlsx
-- (24 task columns + 12 requirement columns; no progress counts, no template/min/max).
--
-- Shape notes:
--   * Same joins as analytics.v_gc_tracker_tasks (raw task -> asset row for asset_status)
--     plus LEFT JOIN stg_gc_tracker_requirements ON task_did.
--   * Name collisions: task last_updated -> task_last_updated, requirement last_updated ->
--     requirement_last_updated (Swift's; does NOT move on file uploads). loaded_at is the
--     requirement row's load time (NULL when the task has no requirements).
--   * files_missing = max(min_file_count - file_uploaded, 0), as on v_gc_tracker_requirements.
--   * RULE for readers: count tasks with count(DISTINCT task_did), never count(*).
--   * Pure view over existing staging tables: no storage, no pipeline change, no new
--     indexes needed (stg_gc_tracker_requirements already has idx on task_did/project_did).
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_task_requirements;
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL
--     AND schema_name = 'analytics' AND table_name = 'v_gc_tracker_task_requirements';
-- =============================================================================

BEGIN;

CREATE VIEW analytics.v_gc_tracker_task_requirements AS
SELECT p.market,
       p.org_name,
       p.project_name,
       t.project_did,
       a.asset_did              AS asset_project_did,
       t.asset_did,
       t.asset_id               AS asset_identifier,
       t.asset_name,
       a.asset_status,
       t.project_status         AS asset_project_status,
       t.task_did,
       t.task_name,
       t.task_name_clean,
       t.task_status,
       t.task_scheduled,
       t.task_assigned_to_name,
       t.task_assigned_to_email,
       t.task_submitted_on,
       t.task_submitted_by_name,
       t.task_approved_on,
       t.task_approved_by_name,
       t.task_cancelled_on,
       t.task_cancelled_by_name,
       t.last_updated           AS task_last_updated,
       q.requirement_did,
       q.requirement_name,
       q.requirement_description,
       q.requirement_status,
       q.file_uploaded,
       q.file_submitted,
       q.file_approved,
       q.file_rejected,
       CASE WHEN q.requirement_did IS NULL THEN NULL
            ELSE GREATEST(COALESCE(q.min_file_count, 0) - COALESCE(q.file_uploaded, 0), 0) END AS files_missing,
       q.created_by_did,
       q.date_created,
       q.last_updated           AS requirement_last_updated,
       q.loaded_at
FROM data_staging.stg_gc_tracker_tasks t
JOIN reference.ref_gc_tracker_projects p ON p.project_did = t.project_did
LEFT JOIN data_raw.raw_gc_tracker_tasks r ON r.task_did = t.task_did
LEFT JOIN data_staging.stg_gc_tracker_assets a ON a.asset_did = r.asset_did
LEFT JOIN data_staging.stg_gc_tracker_requirements q ON q.task_did = t.task_did;

COMMENT ON VIEW analytics.v_gc_tracker_task_requirements IS
    'Combined GC tracker view: one row per requirement with its parent task, site, project and market; tasks without requirements appear once with NULL requirement columns. Filter by market or project_did (7M+ rows). Count tasks with count(DISTINCT task_did). task_last_updated is the change clock; requirement_last_updated does not move on uploads.';

REVOKE ALL ON analytics.v_gc_tracker_task_requirements FROM anon, authenticated;
GRANT SELECT ON analytics.v_gc_tracker_task_requirements TO service_role;
GRANT SELECT ON analytics.v_gc_tracker_task_requirements TO tdt_reader;

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT 'analytics', 'v_gc_tracker_task_requirements', NULL,
       'Combined serving view: GC tracker tasks with their requirements on one row.',
       'The team''s single table for the market trackers: task status/dates/people plus each requirement''s name, status and file counts. Tasks without requirements are kept (requirement columns NULL).',
       'One row per requirement; count tasks with count(DISTINCT task_did). task_last_updated = Swift change clock; requirement_last_updated does not move on uploads; loaded_at is the requirement row''s (UTC). Column pick: reference/gc-tracker/tracker-task-requirements-2026-09-25.xlsx.',
       ARRAY['analytics.v_gc_tracker_tasks', 'analytics.v_gc_tracker_requirements', 'data_staging.stg_gc_tracker_tasks', 'data_staging.stg_gc_tracker_requirements']
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = 'analytics' AND m.table_name = 'v_gc_tracker_task_requirements' AND m.column_name IS NULL
);

COMMIT;
