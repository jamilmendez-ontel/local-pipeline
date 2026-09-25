-- =============================================================================
-- 271_timer_sync.sql
-- Timer sync (ontel-data-platform timer_sync): Swift timer activities mirrored
-- event-driven into shadow tables keyed by Swift's timer id.
-- Spec: ontel-data-platform/docs/superpowers/specs/2026-09-25-timer-sync-design.md
--
-- Why this shape:
--   * stg_timer_activities_inc keeps v1's stg_timer_activities columns (same
--     names, same types) so consumers can be repointed at cutover; PK is Swift's
--     timer id (swift_timer_id); entry_id is v1's row hash, a compatibility
--     column for app_timer.corrections / entry_removals, NOT a key.
--   * Rows are never deleted: vanished_at + vanish_reason ('member_absent' =
--     Swift dropped a deactivated member, keep serving; 'entry_deleted' =
--     the timer is gone from Swift, hide from consumers).
--   * reference.ref_swift_users maps auth0 user ids (the entity feed) to emails
--     (the report feed); learned by the sync, hand-editable.
--   * pipeline.inc_audit_results gains a pipeline column so the timer audit
--     shares the pilot's table.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_timer_entries_live;
--   DROP TABLE IF EXISTS data_staging.stg_timer_activities_inc;
--   DROP TABLE IF EXISTS data_raw.raw_timer_activities_inc;
--   DROP TABLE IF EXISTS reference.ref_swift_users;
--   ALTER TABLE pipeline.inc_audit_results DROP COLUMN IF EXISTS pipeline;
--   DELETE FROM pipeline.content_watermarks WHERE pipeline_name LIKE 'timer/%';
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL AND (
--     (schema_name = 'data_staging' AND table_name = 'stg_timer_activities_inc') OR
--     (schema_name = 'data_raw'     AND table_name = 'raw_timer_activities_inc') OR
--     (schema_name = 'reference'    AND table_name = 'ref_swift_users') OR
--     (schema_name = 'analytics'    AND table_name = 'v_timer_entries_live'));
--
-- STATUS: APPLIED: pending (deploy order step 2, after the platform PR review)
-- =============================================================================

BEGIN;

-- 1. Staging ------------------------------------------------------------------
CREATE TABLE data_staging.stg_timer_activities_inc (
    swift_timer_id   text PRIMARY KEY,
    project          text,
    project_number   integer,
    project_did      text NOT NULL,
    site_name        text,
    site_id          text,
    task             text,
    task_clean       text,
    site_lat         numeric,
    site_long        numeric,
    user_lat         numeric,
    user_long        numeric,
    user_accuracy_m  numeric,
    site_vs_user_km  numeric,
    start_time       timestamptz NOT NULL,
    end_time         timestamptz,
    duration_min     numeric,
    user_name        text,
    user_email       text,
    user_role        text,
    start_date       date NOT NULL,
    end_date         date NOT NULL,
    asset_did        text,
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    user_did         text NOT NULL,
    task_did         text,
    entry_id         text NOT NULL,
    payload_hash     text NOT NULL,
    sync_id          uuid,
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz,
    vanished_at      timestamptz,
    vanish_reason    text CHECK (vanish_reason IN ('member_absent', 'entry_deleted')),
    recheck_misses   smallint NOT NULL DEFAULT 0
);
COMMENT ON TABLE data_staging.stg_timer_activities_inc IS
  'Swift timer activities, event-driven shadow of stg_timer_activities keyed by Swift timer id (ontel-data-platform timer_sync, spec 2026-09-25). Rows are never deleted; see vanish_reason.';
CREATE INDEX idx_stg_timer_inc_project_start ON data_staging.stg_timer_activities_inc (project_did, start_time);
CREATE INDEX idx_stg_timer_inc_email_start   ON data_staging.stg_timer_activities_inc (user_email, start_time);
CREATE INDEX idx_stg_timer_inc_did_start     ON data_staging.stg_timer_activities_inc (user_did, start_time);
CREATE INDEX idx_stg_timer_inc_start_date    ON data_staging.stg_timer_activities_inc (start_date);
CREATE INDEX idx_stg_timer_inc_open          ON data_staging.stg_timer_activities_inc (project_did)
    WHERE end_time IS NULL AND vanished_at IS NULL;
CREATE INDEX idx_stg_timer_inc_entry_id      ON data_staging.stg_timer_activities_inc (entry_id);

-- 2. Raw ----------------------------------------------------------------------
CREATE TABLE data_raw.raw_timer_activities_inc (
    swift_timer_id text PRIMARY KEY,
    project_did    text NOT NULL,
    payload        jsonb NOT NULL,
    payload_hash   text NOT NULL,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE data_raw.raw_timer_activities_inc IS
  'Latest Swift timer entity per timer id (minus the embedded personnel record); rewritten only when the payload hash changes.';

-- 3. User map -----------------------------------------------------------------
CREATE TABLE reference.ref_swift_users (
    user_did      text PRIMARY KEY,
    user_email    text NOT NULL,
    user_name     text,
    source        text NOT NULL DEFAULT 'report_join' CHECK (source IN ('report_join', 'manual')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE reference.ref_swift_users IS
  'Swift auth0 user id -> email. Learned by timer_sync from the timer report (join on name + start second); rows with source = manual are hand-entered.';

-- 4. Audit table gains a pipeline column ----------------------------------------
ALTER TABLE pipeline.inc_audit_results
    ADD COLUMN IF NOT EXISTS pipeline text NOT NULL DEFAULT 'asset_tasks';
CREATE INDEX IF NOT EXISTS idx_inc_audit_results_pipeline_at ON pipeline.inc_audit_results (pipeline, audited_at DESC);

-- 5. Serving view for the member timer page ------------------------------------
CREATE VIEW analytics.v_timer_entries_live AS
SELECT swift_timer_id, entry_id, project_did, project_number, user_did, user_email, user_name,
       start_time, end_time, duration_min, task, task_clean, site_name, site_id, asset_did,
       (end_time IS NULL) AS is_open,
       ((start_time AT TIME ZONE 'Asia/Manila') - interval '6 hours')::date AS shift_day,
       first_seen_at, updated_at, closed_at
FROM data_staging.stg_timer_activities_inc
WHERE vanished_at IS NULL OR vanish_reason = 'member_absent';
COMMENT ON VIEW analytics.v_timer_entries_live IS
  'Live timer entries per member for the timer page: open flag and 06:00 PHT shift day; excludes rows deleted in Swift, keeps deactivated members'' history.';

-- 6. RLS / grants ----------------------------------------------------------------
ALTER TABLE data_staging.stg_timer_activities_inc ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_raw.raw_timer_activities_inc     ENABLE ROW LEVEL SECURITY;
ALTER TABLE reference.ref_swift_users             ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON data_staging.stg_timer_activities_inc FROM anon, authenticated;
REVOKE ALL ON data_raw.raw_timer_activities_inc     FROM anon, authenticated;
REVOKE ALL ON reference.ref_swift_users             FROM anon, authenticated;
REVOKE ALL ON analytics.v_timer_entries_live        FROM anon, authenticated;
GRANT SELECT ON analytics.v_timer_entries_live TO service_role;

-- 7. agent.schema_metadata -------------------------------------------------------
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, NULL, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('data_staging', 'stg_timer_activities_inc',
     'Swift timer activities mirrored event-driven (ontel-data-platform timer_sync), keyed by Swift timer id.',
     'Shadow of stg_timer_activities with the same columns; feeds the member timer page via analytics.v_timer_entries_live. Cutover repoints rebuild_timer_clean() here.',
     'Never deleted: vanish_reason member_absent (deactivated member, keep) vs entry_deleted (gone from Swift, hide). entry_id = v1 row hash for app_timer.* links, not a key. start_date = first day of the ET month of start_time.',
     ARRAY['data_staging.stg_timer_activities', 'data_raw.raw_timer_activities_inc', 'analytics.v_timer_entries_live', 'reference.ref_swift_users']),
    ('data_raw', 'raw_timer_activities_inc',
     'Latest Swift timer entity payload per timer id.',
     'Landing copy for timer_sync; rewritten only when the payload changes.',
     'payload = GET /api/timer-activities object minus the embedded personnel record.',
     ARRAY['data_staging.stg_timer_activities_inc']),
    ('reference', 'ref_swift_users',
     'Swift auth0 user id to email map.',
     'The timer entity feed carries auth0 ids; the report carries emails. Learned once per member by timer_sync.',
     'source = manual rows are hand-entered when the report join cannot match a member.',
     ARRAY['data_staging.stg_timer_activities_inc']),
    ('analytics', 'v_timer_entries_live',
     'Live timer entries per member with open flag and shift day.',
     'Read by the member timer page (sub-project 2). Excludes rows deleted in Swift.',
     'shift_day = Asia/Manila date of (start_time - 6h), the 06:00 PHT shift day used by the Timer Entries email.',
     ARRAY['data_staging.stg_timer_activities_inc'])
) AS v(schema_name, table_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name AND m.column_name IS NULL
);

COMMIT;
