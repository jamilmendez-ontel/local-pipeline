-- =============================================================================
-- 265_gc_tracker_task_metrics.sql
-- GC tracker: task-level progress counts (INIT-747 part 1).
-- Spec: ontel-data-platform/docs/superpowers/specs/2026-09-16-gc-tracker-requirements-design.md
--       section 5.1. Transform: ontel-data-platform platform_core/asset_fields.py TASK_METRIC_MAP.
--
-- Why: every Swift asset-task payload already carries `metrics` with requirement
-- counts by status and file-requirement counts (verified on 20k raw rows: all
-- integer, reqCount absent on ~11%, formRequirement* always 0 today). The market
-- trackers need "how far along is this task" without a requirement-level fetch.
-- No new Swift calls: the walk copies the counts from the payload it already
-- reads, and the guarded upsert rewrites a row only when something moved.
--
-- Column order here == TASK_METRIC_MAP order in the transform (keep in step).
-- req_has_rejection arrives as 0/1 and is stored as boolean.
--
-- DEPLOY ORDER (the walk INSERTs these columns, so they must exist first):
--   1. apply sections 1-3 of this file (DDL, seconds);
--   2. deploy ontel-data-platform with the task_metrics transform;
--   3. run the section-4 backfill AFTER the deploy, per project batch, off-peak.
--      It is idempotent and guarded (rewrites only rows whose counts differ),
--      so a second pass after the first hourly tick costs almost nothing and
--      closes the window between backfill and deploy.
--
-- ROLLBACK:
--   CREATE OR REPLACE VIEW analytics.v_gc_tracker_tasks AS <body from 264>;
--   ALTER TABLE data_staging.stg_gc_tracker_tasks
--     DROP COLUMN req_count, DROP COLUMN req_pending, DROP COLUMN req_in_progress,
--     DROP COLUMN req_submitted, DROP COLUMN req_approved, DROP COLUMN req_rejected,
--     DROP COLUMN req_cancelled, DROP COLUMN req_has_rejection, DROP COLUMN file_req_max,
--     DROP COLUMN file_req_min, DROP COLUMN file_req_uploaded, DROP COLUMN file_req_submitted,
--     DROP COLUMN file_req_approved, DROP COLUMN file_req_rejected, DROP COLUMN form_req_total,
--     DROP COLUMN form_req_uploaded, DROP COLUMN form_req_approved, DROP COLUMN form_req_rejected;
--   (roll the platform image back first or the walk's INSERT fails on the missing columns)
--
-- APPLIED + VERIFIED 2026-09-16 ~22:30 ET against voqfjfngdpcvevbkikud via the Supabase
-- MCP (dry run of DDL + one-project backfill inside ROLLBACK first, then apply_migration
-- sections 1-3). Pre-flight: 265 free on every open PR, view has no dependents, live view
-- body == 264. Post-apply: 18 new columns, view 44 columns, SELECT grants unchanged
-- (postgres, service_role), 2 schema_metadata rows refreshed. ontel-data-platform #13
-- (7b08845) deployed 02:26 UTC, THEN section-4 backfill in 3 calls (batch 0 = 81,275 rows
-- in 22 s; batches 1-5 and 6-10 together ~2 min): 729,535 / 729,535 rows carry counts;
-- req_count NULL on 55,235 (7.6%, Swift omits it), req_has_rejection true on 194,
-- sum(req_count) 6,855,505, sum(req_approved) 2,971,240, sum(file_req_uploaded) 4,107,336.
-- =============================================================================

BEGIN;

-- 1. Columns ----------------------------------------------------------------------
ALTER TABLE data_staging.stg_gc_tracker_tasks
    ADD COLUMN req_count          integer,
    ADD COLUMN req_pending        integer,
    ADD COLUMN req_in_progress    integer,
    ADD COLUMN req_submitted      integer,
    ADD COLUMN req_approved       integer,
    ADD COLUMN req_rejected       integer,
    ADD COLUMN req_cancelled      integer,
    ADD COLUMN req_has_rejection  boolean,
    ADD COLUMN file_req_max       integer,
    ADD COLUMN file_req_min       integer,
    ADD COLUMN file_req_uploaded  integer,
    ADD COLUMN file_req_submitted integer,
    ADD COLUMN file_req_approved  integer,
    ADD COLUMN file_req_rejected  integer,
    ADD COLUMN form_req_total     integer,
    ADD COLUMN form_req_uploaded  integer,
    ADD COLUMN form_req_approved  integer,
    ADD COLUMN form_req_rejected  integer;

COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_count         IS 'Swift metrics.reqCount: requirements on this task (all statuses). NULL when Swift omits it.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_pending       IS 'Swift metrics.reqPending: requirements in status pending.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_in_progress   IS 'Swift metrics.reqInProgress: requirements in status in_progress.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_submitted     IS 'Swift metrics.reqSubmitted: requirements in status submitted.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_approved      IS 'Swift metrics.reqApproved: requirements in status approved.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_rejected      IS 'Swift metrics.reqRejected: requirements in status rejected.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_cancelled     IS 'Swift metrics.reqCancelled: requirements in status cancelled.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.req_has_rejection IS 'Swift metrics.reqHasRejection (0/1 -> boolean): at least one requirement is rejected.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_max      IS 'Swift metrics.fileRequirementMax: sum of maximum file counts over file requirements.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_min      IS 'Swift metrics.fileRequirementMin: sum of minimum file counts (files needed).';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_uploaded IS 'Swift metrics.fileRequirementCurrent: files uploaded across file requirements.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_submitted IS 'Swift metrics.fileRequirementSubmitted: files submitted for review.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_approved IS 'Swift metrics.fileRequirementApproved: files approved.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.file_req_rejected IS 'Swift metrics.fileRequirementRejected: files rejected.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.form_req_total    IS 'Swift metrics.formRequirementTotal: form requirements (0 on every GC row as of 2026-09-16).';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.form_req_uploaded IS 'Swift metrics.formRequirementCurrent.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.form_req_approved IS 'Swift metrics.formRequirementApproved.';
COMMENT ON COLUMN data_staging.stg_gc_tracker_tasks.form_req_rejected IS 'Swift metrics.formRequirementRejected.';

-- 2. Serving view: same body as 264 with the 18 counts appended --------------------
-- (CREATE OR REPLACE keeps the ACLs; columns are appended so existing consumers
--  keep their positions. Live body diffed against 264 before apply: identical.)
CREATE OR REPLACE VIEW analytics.v_gc_tracker_tasks AS
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
       t.form_req_rejected
FROM data_staging.stg_gc_tracker_tasks t
JOIN reference.ref_gc_tracker_projects p ON p.project_did = t.project_did
LEFT JOIN data_raw.raw_gc_tracker_tasks r ON r.task_did = t.task_did
LEFT JOIN data_staging.stg_gc_tracker_assets a ON a.asset_did = r.asset_did;
COMMENT ON VIEW analytics.v_gc_tracker_tasks IS
    'Serving view for the GC market trackers: every GC tracker task with market, org and project names, plus Swift''s requirement/file progress counts (req_*, file_req_*, form_req_*). Filter by market or project_did. loaded_at (UTC) is freshness.';

-- 3. agent.schema_metadata: refresh the data notes -----------------------------------
UPDATE agent.schema_metadata
SET data_notes = 'Same shape as stg_asset_tasks_inc plus 18 progress-count columns (mig 265: req_* by status, file_req_*, form_req_*) copied from Swift''s task metrics; dates are America/New_York calendar dates; loaded_at moves only when a row changes.'
WHERE schema_name = 'data_staging' AND table_name = 'stg_gc_tracker_tasks' AND column_name IS NULL;

UPDATE agent.schema_metadata
SET data_notes = 'asset_project_status is the walk''s project_status column renamed. loaded_at is UTC. req_count/req_pending/req_submitted/req_approved/req_rejected are requirement counts by status on the task; file_req_min vs file_req_uploaded says how many files are still missing (mig 265).'
WHERE schema_name = 'analytics' AND table_name = 'v_gc_tracker_tasks' AND column_name IS NULL;

COMMIT;

-- 4. Backfill (run AFTER the platform deploy, one batch at a time, off-peak) ---------
-- Guarded: rewrites only rows whose stored counts differ from raw, so re-running it
-- is cheap. Run per project (or per market via the allowlist) to keep each
-- transaction short; the hourly walk's upserts on the same rows just wait.
-- Expect ~729k rows on the first pass (~0.3 GB WAL), a few hundred on a re-run.
--
-- UPDATE data_staging.stg_gc_tracker_tasks s
-- SET req_count          = (m->>'reqCount')::int,
--     req_pending        = (m->>'reqPending')::int,
--     req_in_progress    = (m->>'reqInProgress')::int,
--     req_submitted      = (m->>'reqSubmitted')::int,
--     req_approved       = (m->>'reqApproved')::int,
--     req_rejected       = (m->>'reqRejected')::int,
--     req_cancelled      = (m->>'reqCancelled')::int,
--     req_has_rejection  = (m->>'reqHasRejection')::int::boolean,
--     file_req_max       = (m->>'fileRequirementMax')::int,
--     file_req_min       = (m->>'fileRequirementMin')::int,
--     file_req_uploaded  = (m->>'fileRequirementCurrent')::int,
--     file_req_submitted = (m->>'fileRequirementSubmitted')::int,
--     file_req_approved  = (m->>'fileRequirementApproved')::int,
--     file_req_rejected  = (m->>'fileRequirementRejected')::int,
--     form_req_total     = (m->>'formRequirementTotal')::int,
--     form_req_uploaded  = (m->>'formRequirementCurrent')::int,
--     form_req_approved  = (m->>'formRequirementApproved')::int,
--     form_req_rejected  = (m->>'formRequirementRejected')::int
-- FROM (SELECT task_did, data->'metrics' AS m
--       FROM data_raw.raw_gc_tracker_tasks WHERE project_did = ANY(:project_dids)) r
-- WHERE s.task_did = r.task_did
--   AND (s.req_count, s.req_pending, s.req_in_progress, s.req_submitted, s.req_approved,
--        s.req_rejected, s.req_cancelled, s.req_has_rejection, s.file_req_max, s.file_req_min,
--        s.file_req_uploaded, s.file_req_submitted, s.file_req_approved, s.file_req_rejected,
--        s.form_req_total, s.form_req_uploaded, s.form_req_approved, s.form_req_rejected)
--       IS DISTINCT FROM
--       ((m->>'reqCount')::int, (m->>'reqPending')::int, (m->>'reqInProgress')::int,
--        (m->>'reqSubmitted')::int, (m->>'reqApproved')::int, (m->>'reqRejected')::int,
--        (m->>'reqCancelled')::int, (m->>'reqHasRejection')::int::boolean,
--        (m->>'fileRequirementMax')::int, (m->>'fileRequirementMin')::int,
--        (m->>'fileRequirementCurrent')::int, (m->>'fileRequirementSubmitted')::int,
--        (m->>'fileRequirementApproved')::int, (m->>'fileRequirementRejected')::int,
--        (m->>'formRequirementTotal')::int, (m->>'formRequirementCurrent')::int,
--        (m->>'formRequirementApproved')::int, (m->>'formRequirementRejected')::int);
--
-- Verify: SELECT count(*) FILTER (WHERE req_count IS NULL) AS null_req_count, count(*)
--         FROM data_staging.stg_gc_tracker_tasks;   -- expect ~11% NULL (Swift omits reqCount)
--         and per project: our sum(req_count) vs Swift's project metric reqCount (spec section 2).
