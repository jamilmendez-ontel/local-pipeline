-- migrations/052_partition_raw_asset_tasks.sql
-- Convert raw_asset_tasks to a partitioned table (by project_did).
-- Each TS project gets its own partition for independent index management
-- and per-project safety checks.
--
-- MUST be run during maintenance window (pipeline not running).
-- Expected runtime: 2-5 minutes for ~2.5M rows.
--
-- Renumbered from 045 (per the original 2026-04-16 plan) to 052 because
-- 045 is no longer available — migrations 046+ already shipped.
--
-- Originally specified in:
--   docs/plans/2026-04-16-asset-tasks-gha-migration.md (Task 1)

BEGIN;

-- 1. Rename the old table
ALTER TABLE data_raw.raw_asset_tasks RENAME TO raw_asset_tasks_old;

-- 2. Drop old indexes (they reference the old table)
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_loaded_at;
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_run_id;
DROP INDEX IF EXISTS data_raw.idx_raw_asset_tasks_project_did;

-- 3. Create the new partitioned table (same schema, no BIGSERIAL — partitioned tables
--    can't have SERIAL PKs that span partitions. Use GENERATED ALWAYS instead.)
CREATE TABLE data_raw.raw_asset_tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    project_did TEXT NOT NULL,
    data JSONB NOT NULL
) PARTITION BY LIST (project_did);

-- 4. Create a DEFAULT partition to catch any unknown project_did values.
--    New projects land here until a dedicated partition is created at next run.
CREATE TABLE data_raw.raw_asset_tasks_default
    PARTITION OF data_raw.raw_asset_tasks DEFAULT;

-- 5. Create partitions for each known project.
--    project_did values from reference.ref_ontel_techops_projects where project_number >= 13.
CREATE TABLE data_raw.raw_asset_tasks_ts13
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-NFkG865XjMXlwqZ1AqU');

CREATE TABLE data_raw.raw_asset_tasks_ts14
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-NV5j_QcTmdwoaGklFvf');

CREATE TABLE data_raw.raw_asset_tasks_ts15
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-Np5nDzlfJrK_nt5Ro7e');

CREATE TABLE data_raw.raw_asset_tasks_ts16
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-O99xSQdLiGywc6KRVw-');

CREATE TABLE data_raw.raw_asset_tasks_ts17
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-ONLJdAstPfeGwVNgpYH');

CREATE TABLE data_raw.raw_asset_tasks_ts18
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-O_IpQNpLVwhdVC3QYIm');

CREATE TABLE data_raw.raw_asset_tasks_ts19
    PARTITION OF data_raw.raw_asset_tasks
    FOR VALUES IN ('-OmzvGwfYsSskngv6SEo');

-- 6. Create per-partition indexes (fast — each partition is ~350-430K rows)
DO $$
DECLARE
    parts TEXT[] := ARRAY[
        'raw_asset_tasks_ts13', 'raw_asset_tasks_ts14', 'raw_asset_tasks_ts15',
        'raw_asset_tasks_ts16', 'raw_asset_tasks_ts17', 'raw_asset_tasks_ts18',
        'raw_asset_tasks_ts19', 'raw_asset_tasks_default'
    ];
    p TEXT;
BEGIN
    FOREACH p IN ARRAY parts LOOP
        EXECUTE format('CREATE INDEX idx_%s_loaded_at ON data_raw.%I (loaded_at DESC)', p, p);
        EXECUTE format('CREATE INDEX idx_%s_run_id ON data_raw.%I (run_id)', p, p);
    END LOOP;
END $$;

-- 7. Migrate data from old table to new partitioned table.
--    PostgreSQL auto-routes each row to the correct partition by project_did.
INSERT INTO data_raw.raw_asset_tasks (loaded_at, run_id, project_did, data)
SELECT loaded_at, run_id, project_did, data
FROM data_raw.raw_asset_tasks_old;

-- 8. Drop old table
DROP TABLE data_raw.raw_asset_tasks_old;

COMMIT;
