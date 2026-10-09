-- =============================================================================
-- 278_gc_tracker_presence.sql
-- "Gone from Swift" becomes visible and is never deleted (Jamil, 2026-10-09).
-- Companion of ontel-data-platform #28 (9f79ce0), which makes the hourly GC
-- tracker job mark an org/project the GC side removed from Swift DORMANT
-- (pipeline.content_watermarks row gc_tracker_gone/<project_did>, updated_at =
-- since) instead of failing and alerting every hour.
--
-- 1. data_staging.stg_organizations / stg_projects gain missing_since:
--    NULL while the nightly orgs_projects_extract pull returns the row, else the
--    start of the first successful pull that did not. The tables upsert and never
--    delete, so until now a vanished org was indistinguishable from a quiet one
--    (46 orgs and 18 projects absent from the 2026-10-08 pull still looked live).
--    transform.py maintains the column from this migration on; the backfill below
--    derives it from pipeline.pipeline_runs. The 33 '-MANUAL_%' placeholder orgs
--    were never in Swift and stay NULL.
-- 2. analytics.v_gc_tracker_tasks / v_gc_tracker_requirements /
--    v_gc_tracker_task_requirements gain in_swift + missing_since (from the job's
--    dormant marker) and EXCLUDE projects dormant for more than 30 days (Jamil's
--    "recycle bin": hidden, kept). New *_all views keep every row.
--
-- Preflight 2026-10-09 (voqfjfngdpcvevbkikud): 278 free (dir + open PRs; #81 holds
-- 272 which is already applied). pg_depend: nothing depends on the three GC tracker
-- views; the staging tables' dependents (analytics.v_asset_tasks, v_user_priorities,
-- v_asset_gc, v_timer_activities, v_qa_forms, mv_project_summary, mv_daily_completion,
-- reference.ref_ontel_techops_projects) are untouched by ADD COLUMN. Live view bodies
-- diffed against 265/266/272 before the rewrite (identical). Baseline
-- EXPLAIN ANALYZE of `count(*) FROM v_gc_tracker_tasks WHERE market = 'VZW/MP'`:
-- 3.7 s cold, parallel seq scan of stg_gc_tracker_tasks + hash on the allowlist;
-- the added hash on the (tiny) marker set does not change the shape.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_task_requirements;
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_requirements;
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_tasks;
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_task_requirements_all;
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_requirements_all;
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_tasks_all;
--   -- then re-run the CREATE VIEW bodies of 265 (tasks), 266 (requirements), 272
--   -- (task_requirements) and their GRANTs;
--   ALTER TABLE data_staging.stg_organizations DROP COLUMN IF EXISTS missing_since;
--   ALTER TABLE data_staging.stg_projects      DROP COLUMN IF EXISTS missing_since;
--   DELETE FROM agent.schema_metadata WHERE schema_name = 'analytics'
--     AND table_name LIKE 'v_gc_tracker_%_all' AND column_name IS NULL;
--   DELETE FROM agent.schema_metadata WHERE column_name = 'missing_since'
--     AND table_name IN ('stg_organizations', 'stg_projects');
--   (deploy the transform.py without the missing_since UPDATEs first)
-- =============================================================================
BEGIN;
SET LOCAL lock_timeout = '5s';

-- 1. presence on the nightly pull tables -----------------------------------------
ALTER TABLE data_staging.stg_organizations ADD COLUMN IF NOT EXISTS missing_since timestamptz;
ALTER TABLE data_staging.stg_projects      ADD COLUMN IF NOT EXISTS missing_since timestamptz;

COMMENT ON COLUMN data_staging.stg_organizations.missing_since IS
  'NULL while the nightly orgs_projects_extract pull returns this org; else the start of the first successful pull that did not (the org was removed from Swift). Maintained by transform.py; never deleted.';
COMMENT ON COLUMN data_staging.stg_projects.missing_since IS
  'NULL while the nightly orgs_projects_extract pull returns this project; else the start of the first successful pull that did not (the project was removed from Swift). Maintained by transform.py; never deleted.';

WITH runs AS (
    SELECT run_id, started_at
    FROM pipeline.pipeline_runs
    WHERE pipeline_name = 'orgs_projects_extract' AND status = 'success'
      AND completed_at IS NOT NULL
), latest AS (
    SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1
)
UPDATE data_staging.stg_organizations o
   SET missing_since = COALESCE(
         (SELECT min(r2.started_at)
          FROM runs r1 JOIN runs r2 ON r2.started_at > r1.started_at
          WHERE r1.run_id = o.run_id),
         now())
 WHERE o.run_id IS DISTINCT FROM (SELECT run_id FROM latest)
   AND o.org_did NOT LIKE '-MANUAL\_%'
   AND o.missing_since IS NULL;

WITH runs AS (
    SELECT run_id, started_at
    FROM pipeline.pipeline_runs
    WHERE pipeline_name = 'orgs_projects_extract' AND status = 'success'
      AND completed_at IS NOT NULL
), latest AS (
    SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1
)
UPDATE data_staging.stg_projects p
   SET missing_since = COALESCE(
         (SELECT min(r2.started_at)
          FROM runs r1 JOIN runs r2 ON r2.started_at > r1.started_at
          WHERE r1.run_id = p.run_id),
         now())
 WHERE p.run_id IS DISTINCT FROM (SELECT run_id FROM latest)
   AND p.missing_since IS NULL;

-- 2. GC tracker serving views: in_swift / missing_since, 30-day hide, *_all -------
-- The dormant marker set is tiny (one row per dormant project); the pre-filtered
-- subquery lets the planner hash it once.
CREATE OR REPLACE VIEW analytics.v_gc_tracker_tasks_all AS
SELECT p.market,
       p.org_name,
       p.project_name,
       t.project_did,
       a.asset_did AS asset_project_did,
       t.asset_did,
       t.asset_id AS asset_identifier,
       t.asset_name,
       a.asset_status,
       t.project_status AS asset_project_status,
       t.asset_requirement_count,
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
       t.last_updated,
       t.loaded_at,
       t.req_count,
       t.req_pending,
       t.req_in_progress,
       t.req_submitted,
       t.req_approved,
       t.req_rejected,
       t.req_cancelled,
       t.req_has_rejection,
       t.file_req_max,
       t.file_req_min,
       t.file_req_uploaded,
       t.file_req_submitted,
       t.file_req_approved,
       t.file_req_rejected,
       t.form_req_total,
       t.form_req_uploaded,
       t.form_req_approved,
       t.form_req_rejected,
       (g.missing_since IS NULL) AS in_swift,
       g.missing_since
FROM data_staging.stg_gc_tracker_tasks t
JOIN reference.ref_gc_tracker_projects p ON p.project_did = t.project_did
LEFT JOIN data_raw.raw_gc_tracker_tasks r ON r.task_did = t.task_did
LEFT JOIN data_staging.stg_gc_tracker_assets a ON a.asset_did = r.asset_did
LEFT JOIN (SELECT replace(pipeline_name, 'gc_tracker_gone/', '') AS project_did,
                  updated_at AS missing_since
           FROM pipeline.content_watermarks
           WHERE pipeline_name LIKE 'gc\_tracker\_gone/%') g ON g.project_did = t.project_did;

CREATE OR REPLACE VIEW analytics.v_gc_tracker_tasks AS
SELECT * FROM analytics.v_gc_tracker_tasks_all
WHERE missing_since IS NULL OR missing_since > now() - interval '30 days';

CREATE OR REPLACE VIEW analytics.v_gc_tracker_requirements_all AS
SELECT p.market,
       p.org_name,
       p.project_name,
       r.project_did,
       r.asset_project_did,
       r.asset_did,
       t.asset_id AS asset_identifier,
       t.asset_name,
       t.project_status AS asset_project_status,
       r.task_did,
       t.task_name,
       t.task_name_clean,
       t.task_status,
       r.requirement_did,
       r.requirement_template_did,
       r.requirement_name,
       r.requirement_description,
       r.requirement_status,
       r.min_file_count,
       r.max_file_count,
       r.count_is_enforced,
       r.file_uploaded,
       r.file_submitted,
       r.file_approved,
       r.file_rejected,
       GREATEST(COALESCE(r.min_file_count, 0) - COALESCE(r.file_uploaded, 0), 0) AS files_missing,
       r.created_by_did,
       r.date_created,
       r.last_updated,
       r.loaded_at,
       (g.missing_since IS NULL) AS in_swift,
       g.missing_since
FROM data_staging.stg_gc_tracker_requirements r
JOIN reference.ref_gc_tracker_projects p ON p.project_did = r.project_did
LEFT JOIN data_staging.stg_gc_tracker_tasks t ON t.task_did = r.task_did
LEFT JOIN (SELECT replace(pipeline_name, 'gc_tracker_gone/', '') AS project_did,
                  updated_at AS missing_since
           FROM pipeline.content_watermarks
           WHERE pipeline_name LIKE 'gc\_tracker\_gone/%') g ON g.project_did = r.project_did;

CREATE OR REPLACE VIEW analytics.v_gc_tracker_requirements AS
SELECT * FROM analytics.v_gc_tracker_requirements_all
WHERE missing_since IS NULL OR missing_since > now() - interval '30 days';

CREATE OR REPLACE VIEW analytics.v_gc_tracker_task_requirements_all AS
SELECT p.market,
       p.org_name,
       p.project_name,
       t.project_did,
       a.asset_did AS asset_project_did,
       t.asset_did,
       t.asset_id AS asset_identifier,
       t.asset_name,
       a.asset_status,
       t.project_status AS asset_project_status,
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
       t.last_updated AS task_last_updated,
       q.requirement_did,
       q.requirement_name,
       q.requirement_description,
       q.requirement_status,
       q.file_uploaded,
       q.file_submitted,
       q.file_approved,
       q.file_rejected,
       CASE
           WHEN q.requirement_did IS NULL THEN NULL::integer
           ELSE GREATEST(COALESCE(q.min_file_count, 0) - COALESCE(q.file_uploaded, 0), 0)
       END AS files_missing,
       q.created_by_did,
       q.date_created,
       q.last_updated AS requirement_last_updated,
       q.loaded_at,
       (g.missing_since IS NULL) AS in_swift,
       g.missing_since
FROM data_staging.stg_gc_tracker_tasks t
JOIN reference.ref_gc_tracker_projects p ON p.project_did = t.project_did
LEFT JOIN data_raw.raw_gc_tracker_tasks r ON r.task_did = t.task_did
LEFT JOIN data_staging.stg_gc_tracker_assets a ON a.asset_did = r.asset_did
LEFT JOIN data_staging.stg_gc_tracker_requirements q ON q.task_did = t.task_did
LEFT JOIN (SELECT replace(pipeline_name, 'gc_tracker_gone/', '') AS project_did,
                  updated_at AS missing_since
           FROM pipeline.content_watermarks
           WHERE pipeline_name LIKE 'gc\_tracker\_gone/%') g ON g.project_did = t.project_did;

CREATE OR REPLACE VIEW analytics.v_gc_tracker_task_requirements AS
SELECT * FROM analytics.v_gc_tracker_task_requirements_all
WHERE missing_since IS NULL OR missing_since > now() - interval '30 days';

COMMENT ON VIEW analytics.v_gc_tracker_tasks IS
  'Serving view for the GC market trackers: every GC tracker task with market, org and project names, plus Swift''s requirement/file progress counts (req_*, file_req_*, form_req_*). Filter by market or project_did. loaded_at (UTC) is freshness. in_swift = false and missing_since set when the GC side removed the project from Swift (rows kept as last mirrored); projects missing for more than 30 days are hidden here, see v_gc_tracker_tasks_all.';
COMMENT ON VIEW analytics.v_gc_tracker_tasks_all IS
  'v_gc_tracker_tasks without the 30-day hide: every row, including projects removed from Swift long ago (in_swift = false, missing_since = when).';
COMMENT ON VIEW analytics.v_gc_tracker_requirements IS
  'Serving view for the GC market trackers: every requirement under every mirrored task with site, task, market and project names. Filter by market or project_did; 7M+ rows once seeded. files_missing = max(min_file_count - file_uploaded, 0). in_swift / missing_since as on v_gc_tracker_tasks; projects missing for more than 30 days are hidden here, see v_gc_tracker_requirements_all.';
COMMENT ON VIEW analytics.v_gc_tracker_requirements_all IS
  'v_gc_tracker_requirements without the 30-day hide: every row.';
COMMENT ON VIEW analytics.v_gc_tracker_task_requirements IS
  'Combined GC tracker view: one row per requirement with its parent task, site, project and market; tasks without requirements appear once with NULL requirement columns. Filter by market or project_did (7M+ rows). Count tasks with count(DISTINCT task_did). task_last_updated is the change clock; requirement_last_updated does not move on uploads. in_swift / missing_since as on v_gc_tracker_tasks; projects missing for more than 30 days are hidden here, see v_gc_tracker_task_requirements_all.';
COMMENT ON VIEW analytics.v_gc_tracker_task_requirements_all IS
  'v_gc_tracker_task_requirements without the 30-day hide: every row.';

GRANT SELECT ON analytics.v_gc_tracker_tasks_all             TO service_role, tdt_reader;
GRANT SELECT ON analytics.v_gc_tracker_requirements_all      TO service_role, tdt_reader;
GRANT SELECT ON analytics.v_gc_tracker_task_requirements_all TO service_role, tdt_reader;

-- 3. agent.schema_metadata -----------------------------------------------------------
UPDATE agent.schema_metadata
   SET data_notes = concat_ws(' ', data_notes,
       'in_swift / missing_since (migration 278): false / set when the GC side removed the project from Swift; rows are kept as last mirrored. Projects missing for more than 30 days are hidden here; the _all variant keeps every row.')
 WHERE schema_name = 'analytics' AND column_name IS NULL
   AND table_name IN ('v_gc_tracker_tasks', 'v_gc_tracker_requirements', 'v_gc_tracker_task_requirements');

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, v.column_name, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('analytics', 'v_gc_tracker_tasks_all', NULL,
     'v_gc_tracker_tasks without the 30-day hide: every GC tracker task, including projects removed from Swift.',
     'Audits and history of GC projects the GC side removed from Swift (in_swift = false, missing_since = when).',
     'Same columns as v_gc_tracker_tasks. Prefer v_gc_tracker_tasks for live trackers.',
     'analytics.v_gc_tracker_tasks, pipeline.content_watermarks'),
    ('analytics', 'v_gc_tracker_requirements_all', NULL,
     'v_gc_tracker_requirements without the 30-day hide: every row.',
     'Audits and history of requirements under projects removed from Swift.',
     'Same columns as v_gc_tracker_requirements.',
     'analytics.v_gc_tracker_requirements'),
    ('analytics', 'v_gc_tracker_task_requirements_all', NULL,
     'v_gc_tracker_task_requirements without the 30-day hide: every row.',
     'Audits and history of tasks + requirements under projects removed from Swift.',
     'Same columns as v_gc_tracker_task_requirements.',
     'analytics.v_gc_tracker_task_requirements'),
    ('data_staging', 'stg_organizations', 'missing_since',
     'NULL while the nightly org/project pull returns this org; else when it first did not (removed from Swift).',
     'Tells a vanished org from a quiet one; the table never deletes.',
     'Maintained by transform.py each night; backfilled 2026-10-09 from pipeline.pipeline_runs. -MANUAL_ placeholder orgs stay NULL.',
     'pipeline.pipeline_runs'),
    ('data_staging', 'stg_projects', 'missing_since',
     'NULL while the nightly org/project pull returns this project; else when it first did not (removed from Swift).',
     'Tells a vanished project from a quiet one; the table never deletes.',
     'Maintained by transform.py each night; backfilled 2026-10-09 from pipeline.pipeline_runs.',
     'pipeline.pipeline_runs')
) AS v(schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name
      AND m.column_name IS NOT DISTINCT FROM v.column_name
);

COMMIT;

-- Verify:
--   SELECT count(*) FILTER (WHERE missing_since IS NOT NULL) AS orgs_missing FROM data_staging.stg_organizations;   -- expect 13 (46 absent - 33 manual)
--   SELECT count(*) FILTER (WHERE missing_since IS NOT NULL) AS projects_missing FROM data_staging.stg_projects;     -- expect 18
--   SELECT in_swift, count(DISTINCT project_did) FROM analytics.v_gc_tracker_tasks_all GROUP BY 1;
--   SELECT count(*) FROM analytics.v_gc_tracker_tasks;  -- = _all minus rows of projects missing > 30 days (0 today)
