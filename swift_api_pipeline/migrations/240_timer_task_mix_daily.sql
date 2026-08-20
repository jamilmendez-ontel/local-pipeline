-- 240: per-(member, ET day, task category) timer minutes for the ops report
--
-- The weekly/monthly variance & productivity report (ontel-people
-- docs/superpowers/specs/2026-08-20-ops-report-design.md) shows timed hours
-- split Production / Admin / Post-Fill, the same buckets as the drawer Gantt
-- colors. That classification lives in TS (day-timeline.ts taskColorCategory);
-- this migration ports it to SQL so a month of task mix is one small read
-- instead of a 50k-row timer-entry fetch. A drift-guard test in ontel-people
-- (task-mix.sql-sync.test.ts) calls task_mix_category via RPC and compares
-- against the TS classifier.
--
-- ET day bucketing on start_time (the 228 rule: timer objects attribute to the
-- EASTERN day or they disagree with mv_timer_day_rollup / timed_hours).
-- Completed entries only (end_time > start_time), duration_min > 0.
-- NOTE: minutes are raw duration sums; overlapping timers count in each task
-- (mv_timer_day_rollup merges overlaps, so mix minutes >= rollup minutes).
--
-- Rollback: DROP VIEW analytics.v_timer_task_mix_daily;
--           DROP FUNCTION analytics.task_mix_category(text);

-- Deliberately no SET search_path: a proconfig entry would block inlining into
-- views (same note as 217/222).
CREATE OR REPLACE FUNCTION analytics.task_mix_category(p_task text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT CASE
    WHEN s.core IS NULL OR s.core = '' THEN 'standard'
    WHEN s.core LIKE 'data post-fill%' THEN 'postfill'
    WHEN s.core IN (
      'general admin and support',
      'tools and automation',
      'quality assurance and control',
      'training',
      'documentation and standardization',
      'data analysis and scorecarding',
      'shadow session',
      'second level review',
      'r&d initiatives',
      'product development',
      'staffing and human resources',
      'marketing and sales'
    ) THEN 'overhead'
    ELSE 'standard'
  END
  FROM (
    SELECT btrim(regexp_replace(
             regexp_replace(lower(coalesce(p_task, '')), '^\s*\d+[a-z]?\.\s*', ''),
             '\s+', ' ', 'g')) AS core
  ) s
$function$;

COMMENT ON FUNCTION analytics.task_mix_category(text) IS
'Gantt task-type bucket (standard=production, overhead=admin, postfill) for a raw Swift task name. SQL port of ontel-people taskColorCategory (day-timeline.ts); guarded against drift by task-mix.sql-sync.test.ts. Migration 240.';

GRANT EXECUTE ON FUNCTION analytics.task_mix_category(text) TO service_role;

CREATE OR REPLACE VIEW analytics.v_timer_task_mix_daily AS
SELECT
    lower(t.user_email) AS user_email,
    (t.start_time AT TIME ZONE 'America/New_York')::date AS work_date,
    analytics.task_mix_category(t.task) AS category,
    round(sum(t.duration_min)::numeric, 2) AS minutes
FROM data_staging.stg_timer_activities_clean t
WHERE t.user_email IS NOT NULL
  AND t.start_time IS NOT NULL
  AND t.end_time IS NOT NULL
  AND t.end_time > t.start_time
  AND t.duration_min > 0
GROUP BY 1, 2, 3;

COMMENT ON VIEW analytics.v_timer_task_mix_daily IS
'Completed-timer minutes per (member email, ET work day, task-type bucket). Feeds the ops report productivity section. Raw duration sums: overlapping timers count per task. Migration 240.';

GRANT SELECT ON analytics.v_timer_task_mix_daily TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES (
  'analytics', 'v_timer_task_mix_daily',
  'Timer minutes per member per ET day per task-type bucket (standard/overhead/postfill).',
  'Productivity section of the weekly/monthly ops report in ontel-people. standard=Production, overhead=Admin, postfill=Data Post-Fill; classification via analytics.task_mix_category, a port of the app''s Gantt color buckets.',
  ARRAY['data_staging.stg_timer_activities_clean']
)
ON CONFLICT DO NOTHING;
