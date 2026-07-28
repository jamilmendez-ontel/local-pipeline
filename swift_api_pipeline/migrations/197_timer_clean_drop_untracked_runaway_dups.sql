-- Migration 197: rebuild_timer_clean() — drop UNTRACKED same-start runaway duplicates
--
-- Problem: Swift occasionally emits a backdated, already-closed DUPLICATE of a
-- real timer entry — same (project_did, user_email, start_time, site, task) but
-- a much later end_time — because a never-stopped/orphaned timer gets force-
-- closed days later and only then surfaces in the report API, backdated to the
-- original start. It never passes through an "open" state, so the daily digest
-- and duplicate-review flow never catch it, and both the real short entry and
-- the multi-day phantom land in stg_timer_activities_clean.
--
-- Example: Neil De Jesus 2026-07-13 "General Admin and Support" — a real 09:15→
-- 10:02 (47m) entry PLUS a twin at the same 09:15 start that closed 2026-07-22
-- (12,920 min ≈ 215h). The phantom inflated the day-timeline chart AND
-- analytics.v_hr_report_review (timed_hours 215.3, coverage 2692%).
--
-- Fix: after the existing build steps, DROP a runaway (>12h continuous single
-- entry) ONLY WHEN a legitimate (<=12h) twin exists at the exact same start
-- AND no member action covers the group. If a tech corrected, removed, or a
-- duplicate_review touched the group, THAT outcome is authoritative and we
-- leave it untouched (edit-aware). Member-touched groups are already collapsed
-- to a single row by Steps 1-2, so in practice a runaway+twin only coexist when
-- nobody acted; the explicit gates below make that guarantee defensive.
--
-- Threshold 720 min (12h) mirrors the app chart's OPEN_CAP_HOURS: a single
-- continuous timer running longer than a shift is a never-stopped artifact.

CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
BEGIN
    TRUNCATE TABLE data_staging.stg_timer_activities_clean;

    -- Step 1: Insert from staging, excluding rejected duplicates, pending non-max
    -- duplicate entries, and removals (unless a correction supersedes the removal).
    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = COALESCE((rej->>'start_time')::timestamptz, r.start_time)
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (e->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.entry_removals rm
            WHERE t.project_did = rm.project_did
              AND t.user_email  = rm.user_email
              AND t.start_time  = rm.start_time
              AND t.site_name IS NOT DISTINCT FROM rm.site_name
              AND t.site_id   IS NOT DISTINCT FROM rm.site_id
              AND t.task      IS NOT DISTINCT FROM rm.task
              AND t.end_time IS NOT DISTINCT FROM rm.end_time
              AND t.duration_min IS NOT DISTINCT FROM rm.duration_min
              AND rm.reason IS DISTINCT FROM 'REVERTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_timer.corrections c
                  WHERE c.project_did = rm.project_did
                    AND c.user_email  = rm.user_email
                    AND c.start_time  = rm.start_time
                    AND c.site_name IS NOT DISTINCT FROM rm.site_name
                    AND c.site_id   IS NOT DISTINCT FROM rm.site_id
                    AND c.task      IS NOT DISTINCT FROM rm.task
                    AND c.end_time IS NOT DISTINCT FROM rm.end_time
                    AND c.original_duration_min IS NOT DISTINCT FROM rm.duration_min
              )
        )
    ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
             t.task, t.end_time, t.duration_min, t.id;

    -- Step 2: Apply duration corrections (edit-in-place).
    UPDATE data_staging.stg_timer_activities_clean t
    SET duration_min = c.corrected_duration_min,
        end_time    = c.corrected_end_time
    FROM app_timer.corrections c
    WHERE c.status = 'corrected'
      AND t.project_did = c.project_did
      AND t.user_email  = c.user_email
      AND t.start_time  = c.start_time
      AND t.site_name IS NOT DISTINCT FROM c.site_name
      AND t.site_id   IS NOT DISTINCT FROM c.site_id
      AND t.task      IS NOT DISTINCT FROM c.task
      AND t.end_time IS NOT DISTINCT FROM c.end_time
      AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min;

    -- Step 3: Append manual additions (unless removed).
    INSERT INTO data_staging.stg_timer_activities_clean (
        id, project, project_number, project_did, site_name, site_id,
        task, task_clean, site_lat, site_long, user_lat, user_long,
        user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT
        a.id, a.project, a.project_number, a.project_did, a.site_name, a.site_id,
        a.task, a.task_clean, a.site_lat, a.site_long, a.user_lat, a.user_long,
        a.user_accuracy_m, a.site_vs_user_km, a.start_time, a.end_time, a.duration_min,
        a.user_name, a.user_email, a.user_role,
        a.run_id, a.run_date,
        COALESCE(a.start_date, (a.start_time AT TIME ZONE 'America/New_York')::date),
        COALESCE(a.end_date,   (a.start_time AT TIME ZONE 'America/New_York')::date),
        a.loaded_at
    FROM app_timer.entry_additions a
    WHERE NOT EXISTS (
        SELECT 1 FROM app_timer.entry_removals rm
        WHERE rm.project_did = a.project_did
          AND rm.user_email  = a.user_email
          AND rm.start_time  = a.start_time
          AND rm.site_name IS NOT DISTINCT FROM a.site_name
          AND rm.site_id   IS NOT DISTINCT FROM a.site_id
          AND rm.task      IS NOT DISTINCT FROM a.task
          AND rm.end_time IS NOT DISTINCT FROM a.end_time
          AND rm.duration_min IS NOT DISTINCT FROM a.duration_min
          AND rm.reason IS DISTINCT FROM 'REVERTED'
    );

    -- Step 4: Inject orphaned corrections as virtual rows (correction's natural
    -- key no longer matches any staging row after Swift state changed).
    INSERT INTO data_staging.stg_timer_activities_clean (
        project, project_number, project_did, site_name, site_id,
        task, task_clean, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT DISTINCT ON (corr.project_did, corr.user_email, corr.start_time,
                        corr.site_name, corr.site_id, corr.task)
        corr.project,
        NULL::integer AS project_number,
        corr.project_did, corr.site_name, corr.site_id,
        corr.task,
        regexp_replace(corr.task, '^\d+\.\s+', '') AS task_clean,
        corr.start_time, corr.corrected_end_time, corr.corrected_duration_min,
        nm.user_name, corr.user_email, nm.user_role,
        '00000000-0000-0000-0000-000000000002'::uuid AS run_id,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS run_date,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS start_date,
        (COALESCE(corr.corrected_end_time, corr.start_time)
            AT TIME ZONE 'America/New_York')::date AS end_date,
        NOW() AS loaded_at
    FROM app_timer.corrections corr
    LEFT JOIN (
        SELECT user_email,
               (array_agg(user_name ORDER BY start_time DESC)
                  FILTER (WHERE user_name IS NOT NULL AND user_name <> ''))[1] AS user_name,
               (array_agg(user_role ORDER BY start_time DESC)
                  FILTER (WHERE user_role IS NOT NULL AND user_role <> ''))[1] AS user_role
        FROM data_staging.stg_timer_activities
        GROUP BY user_email
    ) nm ON nm.user_email = corr.user_email
    WHERE corr.status = 'corrected'
      AND NOT EXISTS (
          SELECT 1 FROM data_staging.stg_timer_activities_clean t
          WHERE t.project_did = corr.project_did
            AND t.user_email  = corr.user_email
            AND t.start_time  = corr.start_time
            AND t.site_name IS NOT DISTINCT FROM corr.site_name
            AND t.site_id   IS NOT DISTINCT FROM corr.site_id
            AND t.task      IS NOT DISTINCT FROM corr.task
            AND t.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND t.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = corr.project_did
            AND rm.user_email  = corr.user_email
            AND rm.start_time  = corr.start_time
            AND rm.site_name IS NOT DISTINCT FROM corr.site_name
            AND rm.site_id   IS NOT DISTINCT FROM corr.site_id
            AND rm.task      IS NOT DISTINCT FROM corr.task
            AND rm.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND rm.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
            AND rm.reason IS DISTINCT FROM 'REVERTED'
      );

    -- Step 5 (migration 197): drop UNTRACKED same-start runaway duplicates.
    -- A runaway (>12h single entry) is deleted ONLY when a legit (<=12h) twin
    -- exists at the exact same (project_did, user_email, start_time, site, task)
    -- AND no member action (correction / removal / duplicate_review) covers the
    -- group. Member-touched groups are honored as-is (their edit is final).
    DELETE FROM data_staging.stg_timer_activities_clean cln
    WHERE cln.duration_min > 720
      AND EXISTS (
          SELECT 1 FROM data_staging.stg_timer_activities_clean t2
          WHERE t2.project_did = cln.project_did
            AND t2.user_email  = cln.user_email
            AND t2.start_time  = cln.start_time
            AND t2.site_name IS NOT DISTINCT FROM cln.site_name
            AND t2.site_id   IS NOT DISTINCT FROM cln.site_id
            AND t2.task      IS NOT DISTINCT FROM cln.task
            AND t2.duration_min <= 720
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.corrections c
          WHERE c.project_did = cln.project_did
            AND c.user_email  = cln.user_email
            AND c.start_time  = cln.start_time
            AND c.site_name IS NOT DISTINCT FROM cln.site_name
            AND c.site_id   IS NOT DISTINCT FROM cln.site_id
            AND c.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = cln.project_did
            AND rm.user_email  = cln.user_email
            AND rm.start_time  = cln.start_time
            AND rm.site_name IS NOT DISTINCT FROM cln.site_name
            AND rm.site_id   IS NOT DISTINCT FROM cln.site_id
            AND rm.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.duplicate_reviews r
          WHERE r.project_did = cln.project_did
            AND r.user_email  = cln.user_email
            AND r.start_time  = cln.start_time
            AND r.site_name IS NOT DISTINCT FROM cln.site_name
            AND r.site_id   IS NOT DISTINCT FROM cln.site_id
            AND r.task      IS NOT DISTINCT FROM cln.task
      );
END;
$function$;
