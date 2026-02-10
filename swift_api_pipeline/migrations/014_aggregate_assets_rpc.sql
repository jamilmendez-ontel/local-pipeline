-- Migration 014: Create RPC function for server-side asset aggregation
-- Replaces the Python dict aggregation that scans 2.2M rows in 1000-row batches
-- with a single SQL GROUP BY query that runs in ~2 minutes.
-- SET statement_timeout = 120s overrides the PostgREST default (8s) for this function.

CREATE OR REPLACE FUNCTION data_raw.aggregate_assets_from_raw(p_run_id text)
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
    FROM data_raw.raw_asset_tasks r
    WHERE r.run_id = p_run_id::uuid
      AND r.data->>'Asset_DID' IS NOT NULL
    GROUP BY r.project_did, r.data->>'Asset_DID', r.data->>'Asset_ID',
             r.data->>'Asset_Name', r.data->>'Asset_Requirement_Count'
$$;

-- Index on run_id to speed up the WHERE clause (2.2M+ rows)
CREATE INDEX IF NOT EXISTS idx_raw_asset_tasks_run_id
ON data_raw.raw_asset_tasks(run_id);
