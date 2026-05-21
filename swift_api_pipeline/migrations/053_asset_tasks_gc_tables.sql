-- migrations/053_asset_tasks_gc_tables.sql
-- GC asset_tasks pipeline: new raw + staging + analytics objects.
-- Parallel to the Ontel asset_tasks pipeline but covering ~294 non-Ontel orgs.
--
-- ADDITIVE ONLY. No existing tables, MVs, or RPCs are touched.
-- Safe to apply during business hours.
--
-- Spec: docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md
-- Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md (Task 1)
--
-- Design note: only ONE new RPC (data_raw.aggregate_assets_gc) is created.
-- The asset_tasks transform logic lives in Python (transform.py) rather than
-- a SQL RPC, so the GC clone of that logic stays in Python too (Task 3).

BEGIN;

-- =============================================================================
-- 1. RAW TABLE — single unpartitioned (see spec §6 for rationale vs Ontel)
-- =============================================================================

CREATE TABLE data_raw.raw_asset_tasks_gc (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id      UUID NOT NULL,
    org_did     TEXT NOT NULL,
    project_did TEXT NOT NULL,
    data        JSONB NOT NULL
);

-- Write-path indexes: dropped/recreated around bulk load
CREATE INDEX idx_raw_asset_tasks_gc_run_id
    ON data_raw.raw_asset_tasks_gc (run_id);

CREATE INDEX idx_raw_asset_tasks_gc_loaded_at
    ON data_raw.raw_asset_tasks_gc (loaded_at DESC);

-- Cleanup-path index: STAYS UP across bulk loads (only used for cleanup
-- DELETE/COUNT, never on insert hot path). Composite makes per-org
-- cleanup an index scan.
CREATE INDEX idx_raw_asset_tasks_gc_org_did_run_id
    ON data_raw.raw_asset_tasks_gc (org_did, run_id);

-- =============================================================================
-- 2. STAGING TABLES — full mirrors of Ontel staging
-- =============================================================================

CREATE TABLE data_staging.stg_asset_tasks_gc (LIKE data_staging.stg_asset_tasks INCLUDING ALL);
CREATE TABLE data_staging.stg_assets_gc (LIKE data_staging.stg_assets INCLUDING ALL);

-- =============================================================================
-- 3. RPC — aggregate_assets_gc (clone of data_raw.aggregate_assets_from_raw)
--    Differs from original only in: raw_asset_tasks -> raw_asset_tasks_gc
-- =============================================================================

CREATE OR REPLACE FUNCTION data_raw.aggregate_assets_gc(p_run_id text)
RETURNS TABLE (
    project_did text,
    asset_did text,
    asset_id text,
    asset_name text,
    requirement_count int,
    task_count bigint,
    tasks_pending bigint,
    tasks_in_progress bigint,
    tasks_submitted bigint,
    tasks_approved bigint,
    tasks_rejected bigint,
    tasks_cancelled bigint
) LANGUAGE sql STABLE
SET statement_timeout = '120s'
AS $$
    SELECT
        r.project_did,
        r.data->>'Asset_DID' as asset_did,
        r.data->>'Asset_ID' as asset_id,
        r.data->>'Asset_Name' as asset_name,
        (r.data->>'Asset_Requirement_Count')::int as requirement_count,
        COUNT(*) as task_count,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'pending') as tasks_pending,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'in_progress') as tasks_in_progress,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'submitted') as tasks_submitted,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'approved') as tasks_approved,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'rejected') as tasks_rejected,
        COUNT(*) FILTER (WHERE LOWER(r.data->>'Task_Status') = 'cancelled') as tasks_cancelled
    FROM data_raw.raw_asset_tasks_gc r
    WHERE r.run_id = p_run_id::uuid
      AND r.data->>'Asset_DID' IS NOT NULL
    GROUP BY r.project_did, r.data->>'Asset_DID', r.data->>'Asset_ID',
             r.data->>'Asset_Name', r.data->>'Asset_Requirement_Count'
$$;

-- =============================================================================
-- 4. MATERIALIZED VIEWS — _gc variants of the three Ontel MVs
--    Substitutions vs original (migration 021):
--      stg_asset_tasks -> stg_asset_tasks_gc
--      stg_assets       -> stg_assets_gc
--    Shared tables stay the same:
--      stg_projects, stg_timer_activities, stg_qa_form
--    Filter added: WHERE p.org_name != 'Ontel' AND p.org_name NOT LIKE 'Testing%'
--    (otherwise MVs would include Ontel projects with empty GC task stats)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- mv_project_summary_gc
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_project_summary_gc AS
WITH task_stats AS (
    SELECT
        project_did,
        COUNT(*) AS total_tasks,
        COUNT(*) FILTER (WHERE task_status = 'approved') AS tasks_approved,
        COUNT(*) FILTER (WHERE task_status = 'pending') AS tasks_pending,
        COUNT(*) FILTER (WHERE task_status = 'in_progress') AS tasks_in_progress,
        COUNT(*) FILTER (WHERE task_status = 'submitted') AS tasks_submitted,
        COUNT(*) FILTER (WHERE task_status = 'rejected') AS tasks_rejected,
        COUNT(*) FILTER (WHERE task_status = 'cancelled') AS tasks_cancelled,
        MIN(task_approved_on) AS first_approved_date,
        MAX(task_approved_on) AS last_approved_date
    FROM data_staging.stg_asset_tasks_gc
    GROUP BY project_did
),
site_stats AS (
    SELECT
        project_did,
        COUNT(*) AS total_sites
    FROM data_staging.stg_assets_gc
    GROUP BY project_did
),
timer_stats AS (
    SELECT
        project_did,
        ROUND(SUM(duration_min) / 60.0, 1) AS total_hours_logged,
        COUNT(*) AS total_timer_entries,
        COUNT(DISTINCT user_name) AS unique_technicians
    FROM data_staging.stg_timer_activities
    WHERE project_did IS NOT NULL
    GROUP BY project_did
),
qa_stats AS (
    SELECT
        a.project_did,
        COUNT(*) AS total_qa_checks,
        COUNT(*) FILTER (WHERE q.requirement_status = 'approved') AS qa_pass_count,
        COUNT(*) FILTER (WHERE q.requirement_status = 'cancelled') AS qa_fail_count
    FROM data_staging.stg_qa_form q
    JOIN data_staging.stg_assets_gc a ON q.asset_did = a.asset_did
    WHERE q.asset_did IS NOT NULL
    GROUP BY a.project_did
)
SELECT
    p.project_did,
    p.project_name,
    p.org_name,
    p.status AS project_status,
    COALESCE(s.total_sites, 0)::INTEGER AS total_sites,
    COALESCE(t.total_tasks, 0)::INTEGER AS total_tasks,
    COALESCE(t.tasks_approved, 0)::INTEGER AS tasks_approved,
    COALESCE(t.tasks_pending, 0)::INTEGER AS tasks_pending,
    COALESCE(t.tasks_in_progress, 0)::INTEGER AS tasks_in_progress,
    COALESCE(t.tasks_submitted, 0)::INTEGER AS tasks_submitted,
    COALESCE(t.tasks_rejected, 0)::INTEGER AS tasks_rejected,
    COALESCE(t.tasks_cancelled, 0)::INTEGER AS tasks_cancelled,
    CASE WHEN COALESCE(t.total_tasks, 0) > 0
         THEN ROUND(100.0 * COALESCE(t.tasks_approved, 0) / t.total_tasks, 1)
         ELSE 0 END AS completion_pct,
    COALESCE(tm.total_hours_logged, 0) AS total_hours_logged,
    COALESCE(tm.total_timer_entries, 0)::INTEGER AS total_timer_entries,
    COALESCE(tm.unique_technicians, 0)::INTEGER AS unique_technicians,
    COALESCE(qa.total_qa_checks, 0)::INTEGER AS total_qa_checks,
    COALESCE(qa.qa_pass_count, 0)::INTEGER AS qa_pass_count,
    COALESCE(qa.qa_fail_count, 0)::INTEGER AS qa_fail_count,
    CASE WHEN COALESCE(qa.total_qa_checks, 0) > 0
         THEN ROUND(100.0 * qa.qa_pass_count / qa.total_qa_checks, 1)
         ELSE NULL END AS qa_pass_rate,
    t.first_approved_date,
    t.last_approved_date
FROM data_staging.stg_projects p
LEFT JOIN site_stats s ON s.project_did = p.project_did
LEFT JOIN task_stats t ON t.project_did = p.project_did
LEFT JOIN timer_stats tm ON tm.project_did = p.project_did
LEFT JOIN qa_stats qa ON qa.project_did = p.project_did
WHERE p.org_name != 'Ontel'
  AND p.org_name NOT LIKE 'Testing%';

CREATE UNIQUE INDEX ON analytics.mv_project_summary_gc (project_did);

-- ---------------------------------------------------------------------------
-- mv_technician_stats_gc
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_technician_stats_gc AS
SELECT
    task_assigned_to_name AS technician_name,
    task_assigned_to_email AS technician_email,
    COUNT(*) AS total_tasks,
    COUNT(*) FILTER (WHERE task_status = 'approved') AS tasks_approved,
    COUNT(*) FILTER (WHERE task_status = 'submitted') AS tasks_submitted,
    COUNT(*) FILTER (WHERE task_status = 'in_progress') AS tasks_in_progress,
    COUNT(*) FILTER (WHERE task_status = 'pending') AS tasks_pending,
    COUNT(*) FILTER (WHERE task_status = 'rejected') AS tasks_rejected,
    COUNT(*) FILTER (WHERE task_status = 'cancelled') AS tasks_cancelled,
    CASE WHEN COUNT(*) > 0
         THEN ROUND(100.0 * COUNT(*) FILTER (WHERE task_status = 'approved') / COUNT(*), 1)
         ELSE 0 END AS completion_rate,
    COUNT(DISTINCT asset_did) AS unique_sites,
    COUNT(DISTINCT project_did) AS unique_projects,
    MIN(task_approved_on) AS first_completion_date,
    MAX(task_approved_on) AS last_completion_date
FROM data_staging.stg_asset_tasks_gc
WHERE task_assigned_to_name IS NOT NULL
GROUP BY task_assigned_to_name, task_assigned_to_email;

CREATE UNIQUE INDEX ON analytics.mv_technician_stats_gc (technician_name, technician_email);

-- ---------------------------------------------------------------------------
-- mv_daily_completion_gc
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_daily_completion_gc AS
SELECT
    at.task_approved_on AS completion_date,
    at.asset_did,
    a.asset_id,
    a.asset_name,
    at.project_did,
    p.project_name,
    at.task_name_clean AS task_type,
    COUNT(*) AS tasks_completed
FROM data_staging.stg_asset_tasks_gc at
JOIN data_staging.stg_assets_gc a
    ON at.asset_did = a.asset_did AND at.project_did = a.project_did
JOIN data_staging.stg_projects p
    ON at.project_did = p.project_did
WHERE at.task_status = 'approved' AND at.task_approved_on IS NOT NULL
GROUP BY at.task_approved_on, at.asset_did, a.asset_id, a.asset_name,
         at.project_did, p.project_name, at.task_name_clean;

CREATE UNIQUE INDEX ON analytics.mv_daily_completion_gc (completion_date, asset_did, project_did, task_type);

COMMIT;
