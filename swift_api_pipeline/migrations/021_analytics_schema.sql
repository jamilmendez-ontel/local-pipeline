-- Migration 021: Analytics schema — pre-joined views & summary materialized views
-- Creates an analytics layer on top of data_staging for the AI agent to query directly.
-- Regular views: flat pre-joined tables for common query patterns
-- Materialized views: pre-computed aggregations refreshed by pipeline

-- =============================================================================
-- 1. SCHEMA SETUP
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS analytics;

GRANT USAGE ON SCHEMA analytics TO anon, authenticated, service_role;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO anon, authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA analytics TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT ALL ON TABLES TO service_role;

-- Expose analytics schema via PostgREST
ALTER ROLE authenticator SET pgrst.db_schemas = 'public, data_raw, data_staging, pipeline, agent, analytics';
NOTIFY pgrst, 'reload config';


-- =============================================================================
-- 2. REGULAR VIEWS (4) — pre-joined flat views
-- =============================================================================

-- ---------------------------------------------------------------------------
-- v_asset_tasks: Tasks + assets + projects + orgs (most common query pattern)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_asset_tasks AS
SELECT
    at.task_did,
    at.task_name_clean,
    at.task_status,
    at.task_scheduled,
    at.task_approved_on,
    at.task_submitted_on,
    at.task_cancelled_on,
    at.task_assigned_to_name,
    at.task_assigned_to_email,
    at.task_submitted_by_name,
    at.task_approved_by_name,
    at.task_cancelled_by_name,
    at.asset_did,
    a.asset_id,
    a.asset_name,
    at.project_did,
    p.project_name,
    o.org_name
FROM data_staging.stg_asset_tasks at
JOIN data_staging.stg_assets a
    ON at.asset_did = a.asset_did AND at.project_did = a.project_did
JOIN data_staging.stg_projects p
    ON at.project_did = p.project_did
JOIN data_staging.stg_organizations o
    ON p.org_did = o.org_did;


-- ---------------------------------------------------------------------------
-- v_timer_activities: Timer + assets + projects
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_timer_activities AS
SELECT
    t.task_clean,
    t.start_time,
    t.end_time,
    t.duration_min,
    t.user_name,
    t.user_email,
    t.user_role,
    t.site_lat,
    t.site_long,
    t.user_lat,
    t.user_long,
    t.site_vs_user_km,
    t.user_accuracy_m,
    t.start_date,
    t.end_date,
    t.asset_did,
    a.asset_id,
    a.asset_name,
    t.project_did,
    p.project_name
FROM data_staging.stg_timer_activities t
LEFT JOIN data_staging.stg_assets a
    ON t.asset_did = a.asset_did
LEFT JOIN data_staging.stg_projects p
    ON t.project_did = p.project_did;


-- ---------------------------------------------------------------------------
-- v_qa_forms: QA form + assets + projects
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_qa_forms AS
SELECT
    q.form_name,
    q.form_id,
    q.task_clean,
    q.requirement,
    q.requirement_status,
    q.crew_lead,
    q.construction_manager,
    q.subcontractor,
    q.site_id,
    q.site_name,
    q.asset_did,
    a.asset_id   AS resolved_site_id,
    a.asset_name AS resolved_site_name,
    p.project_name,
    q.aat,
    q.ret,
    q.sweeps,
    q.pim,
    q.fiber,
    q.pictures,
    q.as_builts
FROM data_staging.stg_qa_form q
LEFT JOIN data_staging.stg_assets a
    ON q.asset_did = a.asset_did
LEFT JOIN data_staging.stg_projects p
    ON a.project_did = p.project_did;


-- ---------------------------------------------------------------------------
-- v_user_priorities: Priorities + assets + projects + orgs
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_user_priorities AS
SELECT
    up.task_did,
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
    o.org_name
FROM data_staging.stg_user_priorities up
LEFT JOIN data_staging.stg_assets a
    ON up.asset_did = a.asset_did
LEFT JOIN data_staging.stg_projects p
    ON up.project_did = p.project_did
LEFT JOIN data_staging.stg_organizations o
    ON up.org_did = o.org_did;


-- =============================================================================
-- 3. MATERIALIZED VIEWS (3) — pre-computed aggregations
-- =============================================================================

-- ---------------------------------------------------------------------------
-- mv_project_summary: Per-project metrics combining all data sources
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_project_summary AS
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
    FROM data_staging.stg_asset_tasks
    GROUP BY project_did
),
site_stats AS (
    SELECT
        project_did,
        COUNT(*) AS total_sites
    FROM data_staging.stg_assets
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
    JOIN data_staging.stg_assets a ON q.asset_did = a.asset_did
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
LEFT JOIN qa_stats qa ON qa.project_did = p.project_did;

CREATE UNIQUE INDEX ON analytics.mv_project_summary (project_did);


-- ---------------------------------------------------------------------------
-- mv_technician_stats: Per-technician task metrics
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_technician_stats AS
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
FROM data_staging.stg_asset_tasks
WHERE task_assigned_to_name IS NOT NULL
GROUP BY task_assigned_to_name, task_assigned_to_email;

CREATE UNIQUE INDEX ON analytics.mv_technician_stats (technician_name, technician_email);


-- ---------------------------------------------------------------------------
-- mv_daily_completion: Daily task completion trends (for charts)
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_daily_completion AS
SELECT
    at.task_approved_on AS completion_date,
    at.asset_did,
    a.asset_id,
    a.asset_name,
    at.project_did,
    p.project_name,
    at.task_name_clean AS task_type,
    COUNT(*) AS tasks_completed
FROM data_staging.stg_asset_tasks at
JOIN data_staging.stg_assets a
    ON at.asset_did = a.asset_did AND at.project_did = a.project_did
JOIN data_staging.stg_projects p
    ON at.project_did = p.project_did
WHERE at.task_status = 'approved' AND at.task_approved_on IS NOT NULL
GROUP BY at.task_approved_on, at.asset_did, a.asset_id, a.asset_name,
         at.project_did, p.project_name, at.task_name_clean;

CREATE UNIQUE INDEX ON analytics.mv_daily_completion (completion_date, asset_did, project_did, task_type);


-- =============================================================================
-- 4. RPC FUNCTION — Refresh all materialized views
-- =============================================================================

CREATE OR REPLACE FUNCTION analytics.refresh_materialized_views()
RETURNS TABLE(view_name TEXT, refresh_time_ms BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET statement_timeout = '300s'
AS $$
DECLARE
    start_ts TIMESTAMPTZ;
    end_ts TIMESTAMPTZ;
BEGIN
    start_ts := clock_timestamp();
    REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary;
    end_ts := clock_timestamp();
    view_name := 'mv_project_summary';
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;

    start_ts := clock_timestamp();
    REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats;
    end_ts := clock_timestamp();
    view_name := 'mv_technician_stats';
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;

    start_ts := clock_timestamp();
    REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion;
    end_ts := clock_timestamp();
    view_name := 'mv_daily_completion';
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$$;

GRANT EXECUTE ON FUNCTION analytics.refresh_materialized_views() TO service_role;


-- Single-MV refresh (workaround for PostgREST timeout on combined refresh)
CREATE OR REPLACE FUNCTION analytics.refresh_one_mv(p_view_name TEXT)
RETURNS TABLE(view_name TEXT, refresh_time_ms BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET statement_timeout = '300s'
AS $$
DECLARE
    start_ts TIMESTAMPTZ;
    end_ts TIMESTAMPTZ;
BEGIN
    start_ts := clock_timestamp();

    IF p_view_name = 'mv_project_summary' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary;
    ELSIF p_view_name = 'mv_technician_stats' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats;
    ELSIF p_view_name = 'mv_daily_completion' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion;
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;

    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$$;

GRANT EXECUTE ON FUNCTION analytics.refresh_one_mv(TEXT) TO service_role;


-- =============================================================================
-- 5. SCHEMA METADATA — for AI agent grounding
-- =============================================================================

-- Table-level metadata
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
-- Views
('analytics', 'v_asset_tasks', NULL,
 'Pre-joined view: tasks + assets + projects + orgs. Use instead of joining stg_asset_tasks manually.',
 'Most common query pattern. ~2.2M rows. Filter by project_name, task_status, task_name_clean for performance. User may say: "tasks", "work items", "what tasks are assigned".'),

('analytics', 'v_timer_activities', NULL,
 'Pre-joined view: timer entries + assets + projects. Use instead of joining stg_timer_activities manually.',
 'GPS-tracked time logs with resolved asset info. ~273K rows. Filter by project_name, user_name, start_date. User may say: "time logs", "hours worked", "labor hours".'),

('analytics', 'v_qa_forms', NULL,
 'Pre-joined view: QA forms + assets + projects. Use instead of joining stg_qa_form manually.',
 'QA checklist items with resolved site info. ~346K rows. resolved_site_id/resolved_site_name come from stg_assets (canonical). User may say: "QA checks", "inspections", "quality".'),

('analytics', 'v_user_priorities', NULL,
 'Pre-joined view: user priorities + assets + projects + orgs. Use instead of joining stg_user_priorities manually.',
 'Task priority queue with resolved asset/project info. User may say: "priorities", "schedule", "planned work".'),

-- Materialized views
('analytics', 'mv_project_summary', NULL,
 'Pre-computed per-project metrics: task counts, completion %, hours, QA stats. Refreshed by pipeline.',
 'One row per project. Use for dashboards, project comparisons, executive summaries. No need to scan 2.2M task rows. User may say: "project summary", "project stats", "how is the project doing", "completion rate".'),

('analytics', 'mv_technician_stats', NULL,
 'Pre-computed per-technician metrics: task counts, completion rate, sites worked. Refreshed by pipeline.',
 'One row per technician. Use for performance reports, workload analysis. User may say: "technician stats", "tech performance", "who completed the most", "worker productivity".'),

('analytics', 'mv_daily_completion', NULL,
 'Pre-computed daily task completion counts by project and task type. Refreshed by pipeline.',
 'Use for trend charts and time-series analysis. One row per date/project/task_type. User may say: "daily completions", "completion trend", "tasks per day", "progress over time".')

ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    updated_at = NOW();


-- Key column-level metadata for analytics views/MVs
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
-- v_asset_tasks key columns
('analytics', 'v_asset_tasks', 'task_name_clean',
 'Normalized task type (AAT, RET, Sweeps, etc.)',
 'Use for grouping. Cleaned version without sequence numbers.'),

('analytics', 'v_asset_tasks', 'task_status',
 'Current task status: pending, in_progress, submitted, approved, rejected, cancelled',
 '"approved" = completed/done/finished. "pending" = not started.'),

('analytics', 'v_asset_tasks', 'asset_id',
 'Resolved site code from stg_assets (canonical)',
 'Resolved via asset_did join. More current than raw task data.'),

('analytics', 'v_asset_tasks', 'asset_name',
 'Resolved site name from stg_assets (canonical)',
 'Resolved via asset_did join. More current than raw task data.'),

('analytics', 'v_asset_tasks', 'project_name',
 'Project name (e.g., TECH-OPS: TS17)',
 'From stg_projects. Filter by this for project-level queries.'),

-- v_qa_forms key columns
('analytics', 'v_qa_forms', 'resolved_site_id',
 'Canonical site code from stg_assets (resolved via asset_did)',
 'More reliable than site_id which comes from form submission text.'),

('analytics', 'v_qa_forms', 'resolved_site_name',
 'Canonical site name from stg_assets (resolved via asset_did)',
 'More reliable than site_name which comes from form submission text.'),

('analytics', 'v_qa_forms', 'requirement_status',
 'Pass/Fail/N/A status for this QA requirement',
 'Use for calculating pass rates. "Pass" and "Fail" are the key values.'),

-- mv_project_summary key columns
('analytics', 'mv_project_summary', 'completion_pct',
 'Percentage of tasks approved out of total tasks',
 'ROUND(100 * tasks_approved / total_tasks, 1). 0 if no tasks.'),

('analytics', 'mv_project_summary', 'total_hours_logged',
 'Total hours from timer entries for this project',
 'Sum of duration_min / 60. From stg_timer_activities.'),

('analytics', 'mv_project_summary', 'qa_pass_rate',
 'Percentage of QA form requirements approved out of total',
 'ROUND(100 * qa_pass_count / total_qa_checks, 1). NULL if no QA data. qa_pass_count = approved, qa_fail_count = cancelled.'),

-- mv_technician_stats key columns
('analytics', 'mv_technician_stats', 'completion_rate',
 'Percentage of assigned tasks that are approved',
 'ROUND(100 * tasks_approved / total_tasks, 1).'),

('analytics', 'mv_technician_stats', 'unique_sites',
 'Number of distinct sites this technician has worked at',
 'Count of unique asset_did values.'),

-- mv_daily_completion key columns
('analytics', 'mv_daily_completion', 'completion_date',
 'Date tasks were approved',
 'Use for x-axis in trend charts. Filter by date range for performance.'),

('analytics', 'mv_daily_completion', 'task_type',
 'Cleaned task name (AAT, RET, Sweeps, etc.)',
 'From task_name_clean. Use for grouping/coloring in charts.'),

('analytics', 'mv_daily_completion', 'tasks_completed',
 'Number of tasks approved on this date for this project/task_type',
 'Aggregated count. Sum across task_types for daily total.')

ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    updated_at = NOW();
