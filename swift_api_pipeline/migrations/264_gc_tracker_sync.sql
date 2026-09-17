-- =============================================================================
-- 264_gc_tracker_sync.sql
-- GC tracker sync: TSC Construction Verizon-market (VZW/OPW, VZW/MP, VZW/CGC)
-- asset-tasks mirrored hourly by the ontel-data-platform job gc-tracker-reconcile.
-- Spec: ontel-data-platform/docs/superpowers/specs/2026-09-16-gc-tracker-sync-design.md
--
-- Why this shape:
--   * Same columns as the swift-sync pilot tables (stg_assets_inc,
--     stg_asset_tasks_inc, raw_asset_tasks_inc) because the same walk code writes
--     both; separate tables because the pilot tables feed the v1 parity audit.
--   * reference.ref_gc_tracker_projects is the project allowlist the job reads;
--     adding a market is an INSERT here.
--   * The view renames the walk's project_status column to asset_project_status
--     (the walk stores the ASSET-PROJECT's status there, a pilot naming quirk).
--   * Watermarks live in pipeline.content_watermarks as gc_tracker/<project_did>.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_gc_tracker_tasks;
--   DROP TABLE IF EXISTS data_staging.stg_gc_tracker_tasks;
--   DROP TABLE IF EXISTS data_staging.stg_gc_tracker_assets;
--   DROP TABLE IF EXISTS data_raw.raw_gc_tracker_tasks;
--   DROP TABLE IF EXISTS reference.ref_gc_tracker_projects;
--   DELETE FROM pipeline.content_watermarks WHERE pipeline_name LIKE 'gc_tracker/%';
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL AND (
--     (schema_name = 'reference'    AND table_name = 'ref_gc_tracker_projects') OR
--     (schema_name = 'data_raw'     AND table_name = 'raw_gc_tracker_tasks') OR
--     (schema_name = 'data_staging' AND table_name = 'stg_gc_tracker_assets') OR
--     (schema_name = 'data_staging' AND table_name = 'stg_gc_tracker_tasks') OR
--     (schema_name = 'analytics'    AND table_name = 'v_gc_tracker_tasks'));
--
-- APPLIED + VERIFIED 2026-09-16 ~02:55 ET against voqfjfngdpcvevbkikud via the
-- Supabase MCP (dry run with ROLLBACK first, then apply_migration, whole file).
-- Pre-flight: 264 free, 0 name collisions. Post-apply: 6 seed rows, view selects
-- 0 rows, 5 schema_metadata rows, RLS on for all 4 tables.
-- =============================================================================

BEGIN;

-- 1. Allowlist -----------------------------------------------------------------
CREATE TABLE reference.ref_gc_tracker_projects (
    project_did  text PRIMARY KEY,
    org_did      text NOT NULL,
    org_name     text NOT NULL,
    project_name text NOT NULL,
    market       text NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    added_at     timestamptz NOT NULL DEFAULT now(),
    notes        text
);
COMMENT ON TABLE reference.ref_gc_tracker_projects IS
    'Projects the gc-tracker-reconcile job walks. market = VZW/OPW, VZW/MP, VZW/CGC. enabled=false retires a project without deleting its rows.';

INSERT INTO reference.ref_gc_tracker_projects (project_did, org_did, org_name, project_name, market, notes) VALUES
    ('-NCvoSShuHnMwymAawxK', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/OPW - Embedded',             'VZW/OPW', 'seed 2026-09-16'),
    ('-OinpJcOAUjl1R2IOTib', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/OPW - NSB Macro',            'VZW/OPW', 'seed 2026-09-16'),
    ('-OHETpPon_dQXuQ5fptO', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/Mountain Plains - Embedded', 'VZW/MP',  'seed 2026-09-16; Swift spells MP as Mountain Plains'),
    ('-Mb7iVV00Z5ZTy-W27jH', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/CGC - Embedded',             'VZW/CGC', 'seed 2026-09-16'),
    ('-NSM2Xk7eRj-Ifjzit7U', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/CGC - NSB Macro',            'VZW/CGC', 'seed 2026-09-16'),
    ('-MSdEk1C6R0fqWVWIkZY', '-LSLnbjcxlr7_LrWCzeH', 'TSC Construction', 'VZW/CGC - Small Cell',           'VZW/CGC', 'seed 2026-09-16');

-- 2. Raw ------------------------------------------------------------------------
CREATE TABLE data_raw.raw_gc_tracker_tasks (
    task_did     text PRIMARY KEY,
    asset_did    text NOT NULL,            -- ASSET-PROJECT id (walk scope key)
    project_did  text NOT NULL,
    data         jsonb NOT NULL,
    last_updated timestamptz,
    loaded_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_raw_gc_tracker_tasks_asset   ON data_raw.raw_gc_tracker_tasks (asset_did);
CREATE INDEX idx_raw_gc_tracker_tasks_project ON data_raw.raw_gc_tracker_tasks (project_did);
COMMENT ON TABLE data_raw.raw_gc_tracker_tasks IS
    'Swift asset-task payloads for GC tracker projects, one row per task_did, rewritten only when the payload changes. asset_did is the asset-project id.';

-- 3. Staging --------------------------------------------------------------------
CREATE TABLE data_staging.stg_gc_tracker_assets (
    asset_did               text PRIMARY KEY,   -- ASSET-PROJECT id
    project_did             text NOT NULL,
    asset_id                text,               -- Swift identifier path (carries the market)
    asset_name              text,
    asset_requirement_count integer,
    last_updated            timestamptz,
    loaded_at               timestamptz NOT NULL DEFAULT now(),
    asset_short_name        text,
    asset_status            text
);
CREATE INDEX idx_stg_gc_tracker_assets_project ON data_staging.stg_gc_tracker_assets (project_did);
COMMENT ON TABLE data_staging.stg_gc_tracker_assets IS
    'Assets (asset-projects) of GC tracker projects; same shape as stg_assets_inc. asset_id is the Swift identifier path e.g. TSC Construction/VZW/OPW/NSB Macro/17388829/Mar 2026.';

CREATE TABLE data_staging.stg_gc_tracker_tasks (
    task_did                    text PRIMARY KEY,
    project_did                 text NOT NULL,
    project_status              text,           -- the ASSET-PROJECT status (pilot quirk; view renames it)
    asset_did                   text NOT NULL,  -- underlying asset.id
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
    loaded_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_stg_gc_tracker_tasks_asset   ON data_staging.stg_gc_tracker_tasks (asset_did);
CREATE INDEX idx_stg_gc_tracker_tasks_project ON data_staging.stg_gc_tracker_tasks (project_did);
COMMENT ON TABLE data_staging.stg_gc_tracker_tasks IS
    'Asset-tasks of GC tracker projects, one row per task_did, guarded upsert (only changed rows rewritten). Dates are America/New_York calendar dates. Same shape as stg_asset_tasks_inc.';

-- 4. Serving view --------------------------------------------------------------
CREATE VIEW analytics.v_gc_tracker_tasks AS
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
       t.loaded_at
FROM data_staging.stg_gc_tracker_tasks t
JOIN reference.ref_gc_tracker_projects p ON p.project_did = t.project_did
LEFT JOIN data_raw.raw_gc_tracker_tasks r ON r.task_did = t.task_did
LEFT JOIN data_staging.stg_gc_tracker_assets a ON a.asset_did = r.asset_did;
COMMENT ON VIEW analytics.v_gc_tracker_tasks IS
    'Serving view for the GC market trackers: every GC tracker task with market, org and project names. Filter by market or project_did. loaded_at (UTC) is freshness.';

-- 5. Security -------------------------------------------------------------------
ALTER TABLE reference.ref_gc_tracker_projects  ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_gc_tracker_tasks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_gc_tracker_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_gc_tracker_tasks  ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON reference.ref_gc_tracker_projects  FROM anon, authenticated;
REVOKE ALL ON data_raw.raw_gc_tracker_tasks      FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_gc_tracker_assets FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_gc_tracker_tasks  FROM anon, authenticated;
REVOKE ALL ON analytics.v_gc_tracker_tasks       FROM anon, authenticated;
GRANT ALL ON reference.ref_gc_tracker_projects  TO service_role;
GRANT ALL ON data_raw.raw_gc_tracker_tasks      TO service_role;
GRANT ALL ON data_staging.stg_gc_tracker_assets TO service_role;
GRANT ALL ON data_staging.stg_gc_tracker_tasks  TO service_role;
GRANT SELECT ON analytics.v_gc_tracker_tasks    TO service_role;

-- 6. agent.schema_metadata ------------------------------------------------------
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, NULL, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('reference', 'ref_gc_tracker_projects',
     'Allowlist of GC (general contractor) Swift projects mirrored for the market trackers.',
     'One row per project with its market (VZW/OPW, VZW/MP, VZW/CGC). Seeded with six TSC Construction projects 2026-09-16.',
     'enabled=false retires a project. Read by ontel-data-platform gc-tracker-reconcile at the start of every run.',
     ARRAY['data_staging.stg_gc_tracker_tasks', 'analytics.v_gc_tracker_tasks']),
    ('data_raw', 'raw_gc_tracker_tasks',
     'Raw Swift asset-task payloads for GC tracker projects.',
     'Landing copy for the GC tracker mirror; rewritten only when the payload changes.',
     'asset_did = asset-project id (walk scope). Hourly job, guarded upsert, keep-list deletes.',
     ARRAY['data_staging.stg_gc_tracker_tasks']),
    ('data_staging', 'stg_gc_tracker_assets',
     'Assets (asset-projects) of GC tracker projects.',
     'Site-level rows; asset_id is the Swift identifier path that carries GC/market/scope/site number/month.',
     'Same shape as stg_assets_inc. asset_did = asset-project id.',
     ARRAY['data_staging.stg_gc_tracker_tasks', 'reference.ref_gc_tracker_projects']),
    ('data_staging', 'stg_gc_tracker_tasks',
     'Asset-tasks of GC tracker projects, one row per task.',
     'Live mirror for the market trackers (no history). project_status holds the ASSET-PROJECT status.',
     'Same shape as stg_asset_tasks_inc; dates are America/New_York calendar dates; loaded_at moves only when a row changes.',
     ARRAY['data_raw.raw_gc_tracker_tasks', 'data_staging.stg_gc_tracker_assets', 'analytics.v_gc_tracker_tasks']),
    ('analytics', 'v_gc_tracker_tasks',
     'Serving view: GC tracker tasks with market, org and project names.',
     'What the market trackers and DARA read. Filter by market.',
     'asset_project_status is the walk''s project_status column renamed. loaded_at is UTC.',
     ARRAY['data_staging.stg_gc_tracker_tasks', 'reference.ref_gc_tracker_projects'])
) AS v(schema_name, table_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name AND m.column_name IS NULL
);

COMMIT;
