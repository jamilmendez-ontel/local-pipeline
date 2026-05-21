-- migrations/054_refresh_one_mv_add_gc.sql
-- Extend analytics.refresh_one_mv to handle the three _gc MVs created in
-- migration 053. Without this, calling refresh_one_mv('mv_project_summary_gc')
-- raises 'Unknown view: ...'.
--
-- Additive change to the function body only. Function signature unchanged.
-- Safe to apply during business hours.
--
-- Spec: docs/superpowers/specs/2026-05-20-asset-tasks-gc-pipeline-design.md
-- Plan: docs/plans/2026-05-20-asset-tasks-gc-pipeline.md (follow-up to Task 1)

BEGIN;

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
    -- GC MVs added by migration 053:
    ELSIF p_view_name = 'mv_project_summary_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary_gc;
    ELSIF p_view_name = 'mv_technician_stats_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats_gc;
    ELSIF p_view_name = 'mv_daily_completion_gc' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion_gc;
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;

    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$$;

COMMIT;
