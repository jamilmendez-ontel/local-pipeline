-- =============================================================================
-- 297_v1_timer_readers_to_clean.sql
-- Retire the v1 Swift timer extract, step 2: the last two readers of v1's
-- data_staging.stg_timer_activities now read stg_timer_activities_clean (My Timers' output
-- since migration 288).
--
-- STATUS: APPLIED 2026-11-25 09:16 UTC (04:16 AM ET), Jamil's go, one transaction through the
--   session pooler with md5 guards (before: view b6679b74a298198cf09462968b704abe, MV
--   09772c38e3e2dc178b81a5851714388d; after: view 7db03b412d4fe4e0d63fdf822b10191e, MV
--   0dfee7d12cfcb83e10e56a36e7dfffe7).
--   VERIFIED: mv_project_summary 1,204 rows (same), unique index present, ACL unchanged
--   (postgres, service_role); v_timer_activities ACL unchanged; analytics.refresh_one_mv
--   ('mv_project_summary') CONCURRENTLY OK in 14.8 s.
--
-- WHY
--   * Since 288 the warehouse timer answer is stg_timer_activities_clean (built from My Timers,
--     which reads timer sync v2). v1's stg_timer_activities is a raw Swift copy, refreshed by
--     the 1:15 AM ET "Pipeline: Timer" extract only to feed these two objects.
--   * Readers found 2026-11-25: analytics.mv_project_summary (timer_stats CTE) and
--     analytics.v_timer_activities. Neither had a query in pg_stat_statements since the 11-20
--     restart (only the nightly refresh). Named in the DARA and local-ai-agent prompts.
--     The revenue MVs already read the clean table. No app reads either object.
--   * Both tables have identical columns, so this is a source swap with the same output columns.
--
-- EFFECT (expected)
--   mv_project_summary.total_hours_logged 535,859 -> 450,049 h (-16%), total_timer_entries
--   445,408 -> 434,512: the clean table caps runaways, keeps duplicates once and applies
--   corrections. 10 of 16 timer projects move > 5%.
--
-- NOTE: v_timer_activities still grants SELECT to anon/authenticated (pre-existing). Harmless
--   today (neither role has USAGE on analytics) but worth revoking in a security pass.
--
-- ROLLBACK: 297_v1_timer_readers_to_clean_ROLLBACK.sql (exact pre-297 definitions). Only valid
--   while v1's stg_timer_activities is still loaded.
-- =============================================================================

CREATE OR REPLACE VIEW analytics.v_timer_activities AS
 SELECT t.task_clean,
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
   FROM ((data_staging.stg_timer_activities_clean t
     LEFT JOIN data_staging.stg_assets a ON ((t.asset_did = a.asset_did)))
     LEFT JOIN data_staging.stg_projects p ON ((t.project_did = p.project_did)));

CREATE MATERIALIZED VIEW analytics.mv_project_summary_new AS
 WITH task_stats AS (
         SELECT stg_asset_tasks.project_did,
            count(*) AS total_tasks,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'approved'::text)) AS tasks_approved,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'pending'::text)) AS tasks_pending,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'in_progress'::text)) AS tasks_in_progress,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'submitted'::text)) AS tasks_submitted,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'rejected'::text)) AS tasks_rejected,
            count(*) FILTER (WHERE (stg_asset_tasks.task_status = 'cancelled'::text)) AS tasks_cancelled,
            min(stg_asset_tasks.task_approved_on) AS first_approved_date,
            max(stg_asset_tasks.task_approved_on) AS last_approved_date
           FROM data_staging.stg_asset_tasks
          GROUP BY stg_asset_tasks.project_did
        ), site_stats AS (
         SELECT stg_assets.project_did,
            count(*) AS total_sites
           FROM data_staging.stg_assets
          GROUP BY stg_assets.project_did
        ), timer_stats AS (
         SELECT stg_timer_activities.project_did,
            round((sum(stg_timer_activities.duration_min) / 60.0), 1) AS total_hours_logged,
            count(*) AS total_timer_entries,
            count(DISTINCT stg_timer_activities.user_name) AS unique_technicians
           FROM data_staging.stg_timer_activities_clean stg_timer_activities
          WHERE (stg_timer_activities.project_did IS NOT NULL)
          GROUP BY stg_timer_activities.project_did
        ), qa_stats AS (
         SELECT a.project_did,
            count(*) AS total_qa_checks,
            count(*) FILTER (WHERE (q.requirement_status = 'approved'::text)) AS qa_pass_count,
            count(*) FILTER (WHERE (q.requirement_status = 'cancelled'::text)) AS qa_fail_count
           FROM (data_staging.stg_qa_form q
             JOIN data_staging.stg_assets a ON ((q.asset_did = a.asset_did)))
          WHERE (q.asset_did IS NOT NULL)
          GROUP BY a.project_did
        )
 SELECT p.project_did,
    p.project_name,
    p.org_name,
    p.status AS project_status,
    (COALESCE(s.total_sites, (0)::bigint))::integer AS total_sites,
    (COALESCE(t.total_tasks, (0)::bigint))::integer AS total_tasks,
    (COALESCE(t.tasks_approved, (0)::bigint))::integer AS tasks_approved,
    (COALESCE(t.tasks_pending, (0)::bigint))::integer AS tasks_pending,
    (COALESCE(t.tasks_in_progress, (0)::bigint))::integer AS tasks_in_progress,
    (COALESCE(t.tasks_submitted, (0)::bigint))::integer AS tasks_submitted,
    (COALESCE(t.tasks_rejected, (0)::bigint))::integer AS tasks_rejected,
    (COALESCE(t.tasks_cancelled, (0)::bigint))::integer AS tasks_cancelled,
        CASE
            WHEN (COALESCE(t.total_tasks, (0)::bigint) > 0) THEN round(((100.0 * (COALESCE(t.tasks_approved, (0)::bigint))::numeric) / (t.total_tasks)::numeric), 1)
            ELSE (0)::numeric
        END AS completion_pct,
    COALESCE(tm.total_hours_logged, (0)::numeric) AS total_hours_logged,
    (COALESCE(tm.total_timer_entries, (0)::bigint))::integer AS total_timer_entries,
    (COALESCE(tm.unique_technicians, (0)::bigint))::integer AS unique_technicians,
    (COALESCE(qa.total_qa_checks, (0)::bigint))::integer AS total_qa_checks,
    (COALESCE(qa.qa_pass_count, (0)::bigint))::integer AS qa_pass_count,
    (COALESCE(qa.qa_fail_count, (0)::bigint))::integer AS qa_fail_count,
        CASE
            WHEN (COALESCE(qa.total_qa_checks, (0)::bigint) > 0) THEN round(((100.0 * (qa.qa_pass_count)::numeric) / (qa.total_qa_checks)::numeric), 1)
            ELSE NULL::numeric
        END AS qa_pass_rate,
    t.first_approved_date,
    t.last_approved_date
   FROM ((((data_staging.stg_projects p
     LEFT JOIN site_stats s ON ((s.project_did = p.project_did)))
     LEFT JOIN task_stats t ON ((t.project_did = p.project_did)))
     LEFT JOIN timer_stats tm ON ((tm.project_did = p.project_did)))
     LEFT JOIN qa_stats qa ON ((qa.project_did = p.project_did)))
WITH DATA;
DROP MATERIALIZED VIEW analytics.mv_project_summary;
ALTER MATERIALIZED VIEW analytics.mv_project_summary_new RENAME TO mv_project_summary;
CREATE UNIQUE INDEX mv_project_summary_project_did_idx ON analytics.mv_project_summary USING btree (project_did);
REVOKE ALL ON analytics.mv_project_summary FROM PUBLIC, anon, authenticated;
GRANT ALL ON analytics.mv_project_summary TO service_role;
