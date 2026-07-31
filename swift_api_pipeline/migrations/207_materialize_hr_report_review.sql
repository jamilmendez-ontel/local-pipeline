-- 207: materialize v_hr_report_review so every consumer reads precomputed rows
-- (Jamil 2026-07-31). APPLIED to prod 2026-07-31 ~6:28a ET. Verified same day:
-- row parity 25,864 = 25,864; hr_review_count() 3,713 ms -> 17 ms;
-- hr_review_page(100) 69 ms; full chained refresh (rollup + this MV) 1.97 s.
--
-- Problem: analytics.v_hr_report_review is a plain view whose computed columns
-- (coverage_pct, is_late_filing, open_timer_count, ...) re-run per-row correlated
-- subqueries on EVERY call: the ref_employees effective-dated lateral and the
-- report_approval_log overlay probe inside v_daily_report_approvals (25,864 loops /
-- ~105k buffers) plus the mv_timer_day_rollup joins (~64k buffers). Measured on an
-- idle DB 2026-07-31: full SELECT = 736 ms / 173,695 shared hits;
-- analytics.hr_review_count() = 3,713 ms; PostgREST direct filtered reads mean
-- 0.5-3.7 s with maxes 7.1-8.0 s against the 8 s statement timeout. During the
-- daily_reports_rolling write window (~2 min of every 5) these reads intermittently
-- 500 with statement timeouts in Ontel DRMC.
--
-- Fix: snapshot the view's exact SELECT into analytics.mv_hr_report_review
-- (unique on task_did — verified 25,864 rows = 25,864 distinct), index the hot
-- sort/filter columns, and redefine v_hr_report_review as a thin pass-through so
-- the RPCs (hr_review_page/count/summary), the ~10 PostgREST column-header filter
-- reads, and the exports all speed up with ZERO app changes. CREATE OR REPLACE
-- keeps the 31-column contract, so analytics.hr_review_page (RETURNS SETOF
-- v_hr_report_review — the only catalog dependent, verified via pg_depend) is
-- untouched. NEVER DROP the view: that would cascade to hr_review_page.
--
-- Refresh: chained into the EXISTING pg_cron jobid 9
-- (refresh_mv_daily_report_task_rollup, */5) by extending
-- analytics.refresh_dr_task_rollup_safe() (migration 203): the task rollup
-- refreshes first, then mv_hr_report_review — guaranteed upstream-first ordering,
-- no new cron slot. Same stale-not-blank guard convention as 202/203: skip when
-- stg_daily_reports is empty. mv_timer_day_rollup refreshes on its own 10-min
-- job; its changes reach this MV on the next 5-min tick.
--
-- Semantics note: is_missing_report / is_matured embed now() and become
-- as-of-last-refresh (<= ~5 min stale on a 49-hour deadline — acceptable).
--
-- Rollback: see the block at the bottom (original viewdef restored verbatim).

-- ---------------------------------------------------------------------------
-- 1) The materialized snapshot. Body = pg_get_viewdef of the live view,
--    captured 2026-07-31 (post-migration-201 definition). WITH DATA so the
--    first CONCURRENTLY refresh works (CONCURRENTLY errors on unpopulated MVs).
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_hr_report_review AS
 SELECT b.emp_id,
    b.employee_name,
    b.email,
    b."position",
    b.carrier_group,
    b.division,
    b.work_date,
    b.task_did,
    b.task_status,
    b.submitted_on_et,
    b.approved_on_et,
    b.clock_in_et,
    b.approval_latency_days,
    b.total_hours AS stated_hours,
    r.first_clock_in IS NOT NULL AS has_time_in,
    round(EXTRACT(epoch FROM t.submitted_on - r.first_clock_in) / 3600.0, 1) AS filing_lag_hours,
    r.first_clock_in + '49:00:00'::interval AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL AND t.submitted_on >= (r.first_clock_in + '49:00:00'::interval) AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_matured,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    COALESCE(tm.entry_count, 0::bigint) AS timer_entry_count,
    th.person_key IS NOT NULL AS has_timer_history,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL THEN round(GREATEST(b.total_hours - 1::numeric, 0::numeric) - tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL AND GREATEST(b.total_hours - 1::numeric, 0::numeric) > 0::numeric THEN round(100.0 * tm.union_min / 60.0 / GREATEST(b.total_hours - 1::numeric, 0::numeric), 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.shift_time_in_pht,
    b.clock_in_late_minutes,
    EXTRACT(dow FROM b.work_date)::smallint AS work_dow,
    GREATEST(b.total_hours - 1::numeric, 0::numeric) AS stated_hours_net
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true
WITH DATA;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY (task_did = grain, verified unique).
CREATE UNIQUE INDEX IF NOT EXISTS mv_hr_report_review_pk
  ON analytics.mv_hr_report_review (task_did);

-- Hot shapes only (MV is ~11 MB; filtered seq scans are already ms-fast — do not
-- over-index, every index adds refresh diff cost):
--   default sort + date-range pagination (ORDER BY work_date, task_did),
CREATE INDEX IF NOT EXISTS idx_mv_hr_review_work_date
  ON analytics.mv_hr_report_review (work_date, task_did);
--   carrier_group multi-select + date range (highest-call PostgREST shape).
CREATE INDEX IF NOT EXISTS idx_mv_hr_review_carrier_date
  ON analytics.mv_hr_report_review (carrier_group, work_date);

-- ---------------------------------------------------------------------------
-- 2) Repoint the serving view. Column list/order/types are byte-identical to the
--    pre-207 view (31 columns), so this is a CREATE OR REPLACE and hr_review_page
--    keeps its rowtype. If this statement errors, the MV drifted — STOP.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
 SELECT emp_id,
    employee_name,
    email,
    "position",
    carrier_group,
    division,
    work_date,
    task_did,
    task_status,
    submitted_on_et,
    approved_on_et,
    clock_in_et,
    approval_latency_days,
    stated_hours,
    has_time_in,
    filing_lag_hours,
    deadline_at,
    is_late_filing,
    evidence_at,
    is_missing_report,
    is_matured,
    timed_hours,
    open_timer_count,
    timer_entry_count,
    has_timer_history,
    variance_hours,
    coverage_pct,
    shift_time_in_pht,
    clock_in_late_minutes,
    work_dow,
    stated_hours_net
   FROM analytics.mv_hr_report_review;

-- ---------------------------------------------------------------------------
-- 3) Guarded refresh helper — same stale-not-blank convention as 202/203.
--    SECURITY DEFINER + pinned timeout because pg_cron executes it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION analytics.refresh_hr_report_review_safe()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET statement_timeout TO '180s'
AS $function$
BEGIN
  -- Never rebuild from an empty base: keep the MV stale (last good rows), never
  -- blank. stg_daily_reports is upsert-maintained so this should never fire;
  -- defense in depth against manual reloads.
  IF NOT EXISTS (SELECT 1 FROM data_staging.stg_daily_reports LIMIT 1) THEN
    RAISE NOTICE 'refresh_hr_report_review_safe: stg_daily_reports is empty, skipping refresh';
    RETURN;
  END IF;
  REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_hr_report_review;
END;
$function$;

-- ---------------------------------------------------------------------------
-- 4) Chain it into the existing 5-min cron path (jobid 9 already calls
--    refresh_dr_task_rollup_safe — migration 203). Upstream task rollup first,
--    then this MV. No cron.job change needed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION analytics.refresh_dr_task_rollup_safe()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET statement_timeout TO '180s'
AS $function$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM data_staging.stg_daily_report_hours LIMIT 1)
     OR NOT EXISTS (SELECT 1 FROM data_staging.stg_daily_report_attendance LIMIT 1) THEN
    RAISE NOTICE 'refresh_dr_task_rollup_safe: a daily-report source is empty, skipping refresh';
  ELSE
    REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_report_task_rollup;
  END IF;
  -- 207: cascade to the HR review snapshot AFTER its upstream rollup. Runs even
  -- when the rollup refresh was skipped (it then reads the last good rollup).
  PERFORM analytics.refresh_hr_report_review_safe();
END;
$function$;

-- ---------------------------------------------------------------------------
-- 5) Access model: match the wrapped view / sibling MVs (analytics is
--    service_role-only; anon/authenticated lack schema USAGE; MVs cannot have
--    RLS — schema lockout is the control). Default ACLs already grant
--    service_role; made explicit for self-documentation.
-- ---------------------------------------------------------------------------
GRANT SELECT ON analytics.mv_hr_report_review TO service_role;
REVOKE ALL ON analytics.mv_hr_report_review FROM anon, authenticated;

-- ---------------------------------------------------------------------------
-- 6) Semantic-layer metadata (DATABASE_ARCHITECTURE standard). WHERE NOT EXISTS,
--    not ON CONFLICT: the unique key (schema_name, table_name, column_name) has
--    NULL column_name for table-level rows, so ON CONFLICT never dedupes (known
--    2026-06-22 gotcha).
-- ---------------------------------------------------------------------------
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
SELECT
  'analytics', 'mv_hr_report_review',
  'Materialized snapshot of the HR daily-report review layer, one row per report task (task_did grain, 31 columns: filing lateness, 49h maturity, timer coverage/variance, clock-in flags). analytics.v_hr_report_review is now a thin pass-through over this MV.',
  'Performance cache for Ontel DRMC DR Monitoring/Approval: the old live view re-ran per-row employee/approval/timer subqueries on every call (hr_review_count 3.7 s, page/filter reads up to 8 s = statement timeout). Refreshed every 5 min by pg_cron job refresh_mv_daily_report_task_rollup via analytics.refresh_dr_task_rollup_safe() -> refresh_hr_report_review_safe() (stale-not-blank guard). is_matured/is_missing_report are as-of-last-refresh.',
  ARRAY['analytics.v_hr_report_review','analytics.v_daily_report_approvals','analytics.mv_daily_report_task_rollup','analytics.mv_timer_day_rollup','data_staging.stg_daily_reports']
WHERE NOT EXISTS (
  SELECT 1 FROM agent.schema_metadata
  WHERE schema_name = 'analytics' AND table_name = 'mv_hr_report_review'
    AND column_name IS NULL
);

-- ---------------------------------------------------------------------------
-- Rollback (run in this order; NEVER DROP the view — hr_review_page cascades):
--   1. CREATE OR REPLACE VIEW analytics.v_hr_report_review AS <original SELECT —
--      verbatim copy stored in phase-a-design/design.md §9 and identical to the
--      MV body in step 1 above>;
--   2. Re-run the migration-203 body of analytics.refresh_dr_task_rollup_safe()
--      (removes the chained call);
--   3. DROP FUNCTION IF EXISTS analytics.refresh_hr_report_review_safe();
--      DROP MATERIALIZED VIEW IF EXISTS analytics.mv_hr_report_review;
--   4. DELETE FROM agent.schema_metadata
--       WHERE schema_name='analytics' AND table_name='mv_hr_report_review';
-- ---------------------------------------------------------------------------
