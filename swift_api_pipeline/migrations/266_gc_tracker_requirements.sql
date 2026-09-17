-- =============================================================================
-- 266_gc_tracker_requirements.sql
-- GC tracker: requirement level (INIT-747 part 2). One row per Swift requirement
-- under every mirrored asset-task, fetched by ontel-data-platform's walk when the
-- parent task's lastUpdated moves (or its metrics.reqCount disagrees with our rows).
-- Spec: ontel-data-platform/docs/superpowers/specs/2026-09-16-gc-tracker-requirements-design.md
-- Plan: ontel-data-platform/docs/superpowers/plans/2026-09-17-gc-tracker-requirements.md
--
-- Shape notes:
--   * asset_project_did = the walk's scope key (asset-project id, same value as
--     raw_gc_tracker_tasks.asset_did); asset_did = the site id (stg_gc_tracker_tasks.asset_did).
--   * requirement_template_did groups the same requirement across sites (Swift's
--     file-requirement template id), like task_name_clean does for tasks.
--   * last_updated is Swift's requirement timestamp, kept for display; it does NOT
--     move on file uploads, so nothing gates on it.
--   * raw data is TRIMMED (no embedded parent task / template body / validStatuses / ETag).
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_requirements;
--   DROP TABLE IF EXISTS data_staging.stg_gc_tracker_requirements;
--   DROP TABLE IF EXISTS data_raw.raw_gc_tracker_requirements;
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL AND (
--     (schema_name = 'data_raw'     AND table_name = 'raw_gc_tracker_requirements') OR
--     (schema_name = 'data_staging' AND table_name = 'stg_gc_tracker_requirements') OR
--     (schema_name = 'analytics'    AND table_name = 'v_gc_tracker_requirements'));
--   (roll the platform image back first; the walk INSERTs into these tables)
--
-- APPLIED + VERIFIED 2026-09-17 ~00:15 ET against voqfjfngdpcvevbkikud via the Supabase
-- MCP (ROLLBACK dry run first). Pre-flight: 266 free (dir + open PRs), 0 name collisions.
-- Post-apply: 2 tables with RLS, view 30 columns, SELECT grants postgres/service_role/
-- tdt_reader, 3 schema_metadata rows. Local proof with ontel-data-platform #15: Spencer
-- VZW/MP Small Cell (3 tasks) 22 rows = task metrics exactly; VZW/OPW NSB Macro (348 tasks)
-- 3,100 rows in 153 s, 0 mismatches vs req_count, the 54 row-less tasks all claim 0.
-- Footprint measured: raw 1,228 B heap + 136 B index per row, staging 435 + 157 B, about
-- 2 KB per requirement all in (the spec estimated 0.55 KB): ~14 GB for the 297 projects.
-- =============================================================================

BEGIN;

-- 1. Raw ------------------------------------------------------------------------
CREATE TABLE data_raw.raw_gc_tracker_requirements (
    requirement_did   text PRIMARY KEY,
    task_did          text NOT NULL,
    asset_project_did text NOT NULL,
    project_did       text NOT NULL,
    data              jsonb NOT NULL,
    last_updated      timestamptz,
    loaded_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_raw_gc_tracker_requirements_task    ON data_raw.raw_gc_tracker_requirements (task_did);
CREATE INDEX idx_raw_gc_tracker_requirements_asset   ON data_raw.raw_gc_tracker_requirements (asset_project_did);
CREATE INDEX idx_raw_gc_tracker_requirements_project ON data_raw.raw_gc_tracker_requirements (project_did);
COMMENT ON TABLE data_raw.raw_gc_tracker_requirements IS
    'Trimmed Swift asset-file-requirement payloads for GC tracker tasks, one row per requirement_did, rewritten only when the payload changes. asset_project_did is the walk scope key.';

-- 2. Staging --------------------------------------------------------------------
CREATE TABLE data_staging.stg_gc_tracker_requirements (
    requirement_did          text PRIMARY KEY,
    task_did                 text NOT NULL,
    asset_project_did        text NOT NULL,
    asset_did                text,
    project_did              text NOT NULL,
    requirement_template_did text,
    requirement_name         text,
    requirement_description  text,
    requirement_status       text,
    min_file_count           integer,
    max_file_count           integer,
    count_is_enforced        boolean,
    file_uploaded            integer,
    file_submitted           integer,
    file_approved            integer,
    file_rejected            integer,
    created_by_did           text,
    date_created             timestamptz,
    last_updated             timestamptz,
    loaded_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_stg_gc_tracker_requirements_task    ON data_staging.stg_gc_tracker_requirements (task_did);
CREATE INDEX idx_stg_gc_tracker_requirements_asset   ON data_staging.stg_gc_tracker_requirements (asset_project_did);
CREATE INDEX idx_stg_gc_tracker_requirements_project ON data_staging.stg_gc_tracker_requirements (project_did);
CREATE INDEX idx_stg_gc_tracker_requirements_status  ON data_staging.stg_gc_tracker_requirements (requirement_status);
COMMENT ON TABLE data_staging.stg_gc_tracker_requirements IS
    'Requirements under GC tracker tasks, one row per requirement_did, guarded upsert. requirement_status: pending, in_progress, submitted, approved, rejected, cancelled. file_uploaded vs min_file_count = files still missing. last_updated is Swift''s and does not move on uploads.';

-- 3. Serving view --------------------------------------------------------------
CREATE VIEW analytics.v_gc_tracker_requirements AS
SELECT p.market,
       p.org_name,
       p.project_name,
       r.project_did,
       r.asset_project_did,
       r.asset_did,
       t.asset_id               AS asset_identifier,
       t.asset_name,
       t.project_status         AS asset_project_status,
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
       r.loaded_at
FROM data_staging.stg_gc_tracker_requirements r
JOIN reference.ref_gc_tracker_projects p ON p.project_did = r.project_did
LEFT JOIN data_staging.stg_gc_tracker_tasks t ON t.task_did = r.task_did;
COMMENT ON VIEW analytics.v_gc_tracker_requirements IS
    'Serving view for the GC market trackers: every requirement under every mirrored task with site, task, market and project names. Filter by market or project_did; 7M+ rows once seeded. files_missing = max(min_file_count - file_uploaded, 0).';

-- 4. Security -------------------------------------------------------------------
ALTER TABLE data_raw.raw_gc_tracker_requirements     ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_gc_tracker_requirements ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON data_raw.raw_gc_tracker_requirements     FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_gc_tracker_requirements FROM anon, authenticated;
REVOKE ALL ON analytics.v_gc_tracker_requirements      FROM anon, authenticated;
GRANT ALL ON data_raw.raw_gc_tracker_requirements      TO service_role;
GRANT ALL ON data_staging.stg_gc_tracker_requirements  TO service_role;
GRANT SELECT ON analytics.v_gc_tracker_requirements    TO service_role;
GRANT SELECT ON data_staging.stg_gc_tracker_requirements TO tdt_reader;
GRANT SELECT ON analytics.v_gc_tracker_requirements    TO tdt_reader;

-- 5. agent.schema_metadata ------------------------------------------------------
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, NULL, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('data_raw', 'raw_gc_tracker_requirements',
     'Trimmed raw Swift requirement payloads under GC tracker tasks.',
     'Landing copy for the requirement level of the GC tracker mirror (INIT-747 part 2).',
     'One row per requirement_did; rewritten only when the trimmed payload changes. asset_project_did = walk scope key.',
     ARRAY['data_staging.stg_gc_tracker_requirements', 'data_raw.raw_gc_tracker_tasks']),
    ('data_staging', 'stg_gc_tracker_requirements',
     'Requirements (name, status, file counts) under every GC tracker task.',
     'What the market trackers read to see which photo/document requirements are still pending per site and task.',
     'Fetched when the parent task lastUpdated moves or reqCount disagrees with our rows; requirement last_updated is Swift''s and unreliable for change detection.',
     ARRAY['data_staging.stg_gc_tracker_tasks', 'analytics.v_gc_tracker_requirements']),
    ('analytics', 'v_gc_tracker_requirements',
     'Serving view: GC tracker requirements with site, task, market and project names.',
     'Requirement-level detail for the market trackers and DARA. Always filter by market or project_did.',
     'files_missing = max(min_file_count - file_uploaded, 0). loaded_at is UTC.',
     ARRAY['data_staging.stg_gc_tracker_requirements', 'analytics.v_gc_tracker_tasks'])
) AS v(schema_name, table_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name AND m.column_name IS NULL
);

COMMIT;
