-- 242: analytics.member_weekly_task_mix(p_from, p_to): completed-timer minutes
-- per (member email, ET ISO week, task-type bucket) for the member weekly pack
--
-- The per-member Timer & Daily Report Summary (ontel-people
-- docs/superpowers/specs/2026-08-25-member-weekly-report-design.md) shows a
-- 5-week task mix (the report week plus the 4 preceding ISO weeks). This is the
-- mix query of the approved prototype (ontel-people
-- out/member-week-samples/extract.py L78-87) as a set-returning function, so
-- the Tuesday cron makes ONE aggregate call for the whole roster instead of
-- reading ~17k timer rows.
--
-- Same basis as analytics.v_timer_task_mix_daily (migration 240): clean table,
-- analytics.task_mix_category(task), ET day bucketing on start_time (the 228
-- rule), completed entries only (end_time > start_time), duration_min > 0.
-- ONE deliberate difference (Jamil 2026-08-24): a single timer longer than
-- 12h (duration_min > 720) is a runaway, not work evidence, and is EXCLUDED
-- here; the pack draws it hatched on the timeline instead.
-- week_start = date_trunc('week', ET date) = the ISO Monday.
-- NOTE: minutes are raw duration sums; overlapping timers count in each task
-- (mv_timer_day_rollup merges overlaps, so mix minutes >= rollup minutes).
-- The caller resolves user_email -> emp_id via reference.ref_employee_emails
-- (latest last_seen wins), the mv_timer_day_rollup rule (migration 170).
--
-- Rollback: DROP FUNCTION analytics.member_weekly_task_mix(date, date);
-- APPLIED + VERIFIED 2026-08-25 ~06:10 ET against voqfjfngdpcvevbkikud via Supabase
-- MCP apply_migration (controller, member weekly plan Task 10/19). Verification 1:
-- almond.sarreal@ontel.co returned the locked 10 rows (2026-07-20 postfill 226.46 ...
-- 2026-08-17 standard 3198.41), identical to mix.json. Verification 2: svc_exec=true,
-- week_rows=184, week_members=80, bad_categories=0, bad_weeks=0, metadata_rows=1.
-- Verification 3 (PostgREST rpc) is exercised by ontel-people's
-- member-week-queries.sql-sync.test.ts with a live env.

-- Deliberately no SET search_path (same note as 217/222/240): schema-qualified
-- throughout, and a proconfig entry would block inlining.
CREATE OR REPLACE FUNCTION analytics.member_weekly_task_mix(p_from date, p_to date)
RETURNS TABLE (user_email text, week_start date, category text, minutes numeric)
LANGUAGE sql
STABLE
AS $function$
  SELECT lower(t.user_email) AS user_email,
         date_trunc('week', (t.start_time AT TIME ZONE 'America/New_York')::date)::date AS week_start,
         analytics.task_mix_category(t.task) AS category,
         round(sum(t.duration_min)::numeric, 2) AS minutes
  FROM data_staging.stg_timer_activities_clean t
  WHERE t.user_email IS NOT NULL
    AND t.start_time IS NOT NULL
    AND t.end_time IS NOT NULL
    AND t.end_time > t.start_time
    AND t.duration_min > 0
    AND t.duration_min <= 12 * 60
    AND (t.start_time AT TIME ZONE 'America/New_York')::date BETWEEN p_from AND p_to
  GROUP BY 1, 2, 3
  ORDER BY 1, 2, 3
$function$;

COMMENT ON FUNCTION analytics.member_weekly_task_mix(date, date) IS
'Completed-timer minutes per (member email, ET ISO week start, task-type bucket standard/overhead/postfill) over ET work dates p_from..p_to, single timers > 720 min excluded (runaway rule). Feeds the member weekly Timer & Daily Report Summary in ontel-people. Raw duration sums: overlapping timers count per task. Migration 242.';

GRANT EXECUTE ON FUNCTION analytics.member_weekly_task_mix(date, date) TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES (
  'analytics', 'member_weekly_task_mix',
  'Function(p_from date, p_to date) -> table(user_email, week_start, category, minutes): completed-timer minutes per member email per ET ISO week per task-type bucket (standard/overhead/postfill), single timers over 12h excluded.',
  'Task-mix section of the per-member weekly Timer & Daily Report Summary packs sent by ontel-people (DRMC) every Tuesday. Same classification as v_timer_task_mix_daily (analytics.task_mix_category); the 12h runaway exclusion is the pack''s rule (a timer left running is not work evidence).',
  ARRAY['data_staging.stg_timer_activities_clean', 'analytics.v_timer_task_mix_daily', 'reference.ref_employee_emails']
)
ON CONFLICT DO NOTHING;

-- =============================================================================
-- Applied 2026-08-25 (see the APPLIED + VERIFIED block in the header). The
-- verification queries below were run verbatim after apply_migration.
--
-- Preflight (before apply_migration) -- prove the function does not exist yet:
--   SELECT * FROM analytics.member_weekly_task_mix('2026-07-20', '2026-08-23') LIMIT 1;
-- Expected: ERROR function analytics.member_weekly_task_mix(unknown, unknown) does not exist
--
-- Post-apply verification 1 -- sample member, locked expected numbers (10 rows,
-- = ontel-people out/member-week-samples/data/mix.json for
-- almond.sarreal@ontel.co, extracted 2026-08-24; a difference of a few minutes
-- on one week is data drift from nightly rebuild_timer_clean(), not a port
-- error -- a missing week, wrong week_start Monday, or a 4th category IS a
-- port error):
--
--   SELECT user_email, week_start, category, minutes
--   FROM analytics.member_weekly_task_mix('2026-07-20', '2026-08-23')
--   WHERE user_email = 'almond.sarreal@ontel.co'
--   ORDER BY week_start, category;
--
-- Expected 10 rows, first 2026-07-20 postfill 226.46, last 2026-08-17 standard 3198.41:
--   almond.sarreal@ontel.co | 2026-07-20 | postfill | 226.46
--   almond.sarreal@ontel.co | 2026-07-20 | standard | 3324.28
--   almond.sarreal@ontel.co | 2026-07-27 | postfill | 96.77
--   almond.sarreal@ontel.co | 2026-07-27 | standard | 1690.80
--   almond.sarreal@ontel.co | 2026-08-03 | postfill | 284.05
--   almond.sarreal@ontel.co | 2026-08-03 | standard | 3308.13
--   almond.sarreal@ontel.co | 2026-08-10 | postfill | 145.43
--   almond.sarreal@ontel.co | 2026-08-10 | standard | 2533.93
--   almond.sarreal@ontel.co | 2026-08-17 | postfill | 166.08
--   almond.sarreal@ontel.co | 2026-08-17 | standard | 3198.41
--
-- Post-apply verification 2 -- grant, shape, and metadata:
--
--   SELECT has_function_privilege('service_role', 'analytics.member_weekly_task_mix(date, date)', 'EXECUTE') AS svc_exec,
--          (SELECT count(*) FROM analytics.member_weekly_task_mix('2026-08-17', '2026-08-23')) AS week_rows,
--          (SELECT count(DISTINCT user_email) FROM analytics.member_weekly_task_mix('2026-08-17', '2026-08-23')) AS week_members,
--          (SELECT count(*) FROM analytics.member_weekly_task_mix('2026-08-17', '2026-08-23')
--            WHERE category NOT IN ('standard', 'overhead', 'postfill')) AS bad_categories,
--          (SELECT count(*) FROM analytics.member_weekly_task_mix('2026-08-17', '2026-08-23')
--            WHERE week_start <> DATE '2026-08-17') AS bad_weeks,
--          (SELECT count(*) FROM agent.schema_metadata WHERE schema_name = 'analytics' AND table_name = 'member_weekly_task_mix') AS metadata_rows;
--
-- Expected: svc_exec = true, week_rows between 100 and 400, week_members
-- between 60 and 130, bad_categories = 0, bad_weeks = 0, metadata_rows = 1.
--
-- Post-apply verification 3 -- PostgREST RPC path (what ontel-people Task 7 uses):
--
--   NOTIFY pgrst, 'reload schema';
--
-- then from PowerShell (values from ontel-people/.env.local,
-- NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
--
--   $key = "<SUPABASE_SERVICE_ROLE_KEY>"
--   Invoke-RestMethod -Method Post -Uri "https://voqfjfngdpcvevbkikud.supabase.co/rest/v1/rpc/member_weekly_task_mix" `
--     -Headers @{ apikey = $key; Authorization = "Bearer $key"; "Accept-Profile" = "analytics"; "Content-Profile" = "analytics"; "Content-Type" = "application/json" } `
--     -Body '{"p_from":"2026-07-20","p_to":"2026-08-23"}' `
--     | Where-Object { $_.user_email -eq "almond.sarreal@ontel.co" } | Select-Object -First 2
--
-- Expected: two objects, first user_email=almond.sarreal@ontel.co
-- week_start=2026-07-20 category=postfill minutes=226.46. If PGRST202 "Could
-- not find the function", wait 30s after the NOTIFY and retry once.
-- =============================================================================
