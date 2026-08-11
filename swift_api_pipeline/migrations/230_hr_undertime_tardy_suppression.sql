-- 230: an undertime DR (leave code 007-UT) is not tardy.
-- (Jamil 2026-08-11: "when the dr have this 007-UT it means that they take
-- undertime ... they should not be flag as late". Case: Aiko Palang 220501
-- 2026-07-31 — shift 10:00 AM PHT, clock-in 3:30 PM PHT, 330m late, but the DR
-- description opens with "007-UT": an approved shorter shift, so the
-- clock-in-vs-roster comparison is meaningless, same as the 215 weekend rule.)
--
-- Detection: members open the DR hour description with the leave-code legend
-- (reference.ref_leave_code: UT = 007 Undertime). Observed spellings over 90
-- days: "007-UT", "007 UT", "007 - UT" — always at the start of
-- stg_daily_report_hours.work_description. Matcher (case-insensitive, anchored
-- so a mid-text mention like "advised her to file for UT" cannot match):
--     ^\s*007\s*-?\s*UT\M
-- A DR has_undertime when ANY of its hour rows matches (join on task_did,
-- probed via the (task_did, req_id) unique index).
--
-- Measured 2026-08-11 pre-apply, work_date >= current_date - 180:
--   tardy rows 1,071; of those with a UT code 30 (all eyeballed as genuine
--   undertime, incl. the Aiko row); UT reports total 114. MV rows 27,206.
-- Expect after: same MV row count, 180d tardy = 1,041, Aiko 07-31 is_tardy
-- false / has_undertime true.
--
-- Scope: is_tardy ONLY. is_late_filing is untouched — undertime shortens the
-- shift, it does not excuse filing the report late. Other leave codes are also
-- untouched: full-day leaves have no clock-in, so they never reach the tardy
-- comparison in the first place.
--
-- Mechanics: the 170/222/223-pattern swap of mv_hr_report_review (body =
-- migration 223's, verbatim, plus a trailing has_undertime EXISTS), the
-- serving view repointed with the identical 38-column list plus is_tardy
-- gaining AND NOT has_undertime and has_undertime appended as column 39, and
-- v_daily_report_approvals.is_tardy (the DR Approval badge, migration 215 §2)
-- patched by anchored replace on pg_get_viewdef so both surfaces keep the one
-- verdict. RPCs untouched: hr_review_page/hr_review_count and the infraction
-- RPCs all read v.is_tardy (215), so they follow automatically.
--
-- Rollback at the bottom. NEVER DROP analytics.v_hr_report_review.

SET LOCAL lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0) Preflight: 223 must be applied, 230 must not be.
-- ---------------------------------------------------------------------------
DO $$
DECLARE cur text;
BEGIN
  cur := pg_get_viewdef('analytics.mv_hr_report_review'::regclass);
  IF position('r.first_clock_in, (21)::numeric' IN cur) = 0 THEN
    RAISE EXCEPTION '230: mv gap lateral does not anchor at first_clock_in; 223 missing or drifted';
  END IF;
  IF position('has_undertime' IN cur) > 0 THEN
    RAISE EXCEPTION '230: mv already has has_undertime; already applied';
  END IF;
  IF to_regclass('analytics.mv_hr_report_review_v2') IS NOT NULL THEN
    RAISE EXCEPTION '230: mv_hr_report_review_v2 already exists; clean up before re-running';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) New review MV: body = migration 223's (the live definition), verbatim,
--    plus the trailing has_undertime column.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_hr_report_review_v2 AS
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
    GREATEST(b.total_hours - 1::numeric, 0::numeric) AS stated_hours_net,
    fts.first_task_start,
    tm.last_end AS last_task_end,
        CASE
            WHEN COALESCE(tm.open_count, 0::bigint) = 0 THEN gap.long_gap_count
            ELSE NULL::bigint
        END AS long_gap_count,
        CASE
            WHEN COALESCE(tm.open_count, 0::bigint) = 0 THEN gap.long_gap_minutes
            ELSE NULL::numeric
        END AS long_gap_minutes,
    ow.early_task_count,
    ow.late_task_count,
    EXISTS ( SELECT 1
           FROM data_staging.stg_daily_report_hours h
          WHERE h.task_did = b.task_did
            AND h.work_description ~* '^\s*007\s*-?\s*UT\M'::text) AS has_undertime
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true
     LEFT JOIN LATERAL ( SELECT min(x.st) AS first_task_start
           FROM unnest(tm.start_times, tm.end_times) x(st, en)
          WHERE x.en IS NULL OR x.en > r.first_clock_in) fts ON tm.person_key IS NOT NULL AND r.first_clock_in IS NOT NULL
     LEFT JOIN LATERAL ( SELECT g.long_gap_count,
            g.long_gap_minutes
           FROM analytics.long_gap_stats(tm.block_starts, tm.block_ends, r.first_clock_in, 21::numeric, r.first_clock_in + make_interval(secs => (b.total_hours * 3600::numeric)::double precision)) g(long_gap_count, long_gap_minutes)) gap ON tm.person_key IS NOT NULL AND fts.first_task_start IS NOT NULL AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
     LEFT JOIN LATERAL ( SELECT count(*) FILTER (WHERE st.st < r.first_clock_in) AS early_task_count,
            count(*) FILTER (WHERE st.st > (r.first_clock_in + make_interval(secs => (b.total_hours * 3600::numeric)::double precision))) AS late_task_count
           FROM unnest(tm.start_times) st(st)) ow ON tm.person_key IS NOT NULL AND r.first_clock_in IS NOT NULL AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
WITH DATA;

CREATE UNIQUE INDEX mv_hr_report_review_v2_pk
  ON analytics.mv_hr_report_review_v2 (task_did);
CREATE INDEX idx_mv_hr_review_v2_work_date
  ON analytics.mv_hr_report_review_v2 (work_date, task_did);
CREATE INDEX idx_mv_hr_review_v2_carrier_date
  ON analytics.mv_hr_report_review_v2 (carrier_group, work_date);

GRANT SELECT ON analytics.mv_hr_report_review_v2 TO service_role;
REVOKE ALL ON analytics.mv_hr_report_review_v2 FROM anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2) Repoint the serving view, drop the old MV, rename v2 into place. Column
--    list identical to 223's (CREATE OR REPLACE requires it); is_tardy gains
--    AND NOT m.has_undertime; has_undertime appended as the new last column.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
 SELECT m.emp_id,
    m.employee_name,
    m.email,
    m."position",
    m.carrier_group,
    m.division,
    m.work_date,
    m.task_did,
    m.task_status,
    m.submitted_on_et,
    m.approved_on_et,
    m.clock_in_et,
    m.approval_latency_days,
    m.stated_hours,
    m.has_time_in,
    m.filing_lag_hours,
    m.deadline_at,
    COALESCE(m.is_late_filing AND m.task_status IS DISTINCT FROM 'cancelled'::text, false) AS is_late_filing,
    m.evidence_at,
    m.is_missing_report,
    m.is_matured,
    m.timed_hours,
    m.open_timer_count,
    m.timer_entry_count,
    m.has_timer_history,
    m.variance_hours,
    m.coverage_pct,
    m.shift_time_in_pht,
    m.clock_in_late_minutes,
    m.work_dow,
    m.stated_hours_net,
    COALESCE(m.clock_in_late_minutes > 30 AND m.task_status IS DISTINCT FROM 'cancelled'::text AND (m.work_dow <> ALL (ARRAY[0, 6])) AND NOT m.has_undertime, false) AS is_tardy,
    m.first_task_start,
    m.last_task_end,
    m.long_gap_count,
    m.long_gap_minutes,
    m.early_task_count,
    m.late_task_count,
    m.has_undertime
   FROM analytics.mv_hr_report_review_v2 m;

COMMENT ON VIEW analytics.v_hr_report_review IS
  'HR daily-report review serving view over mv_hr_report_review. is_tardy per migration 215 (grace 30m, excludes cancelled DRs and Sat/Sun work) narrowed by 230: an undertime DR (has_undertime, leave code 007-UT at the start of any hour description) is never tardy. is_late_filing excludes cancelled DRs and is NOT undertime-suppressed. first_task_start/long_gap_* per 222/223; early/late_task_count per 217.';

DROP MATERIALIZED VIEW analytics.mv_hr_report_review;

ALTER MATERIALIZED VIEW analytics.mv_hr_report_review_v2 RENAME TO mv_hr_report_review;
ALTER INDEX analytics.mv_hr_report_review_v2_pk RENAME TO mv_hr_report_review_pk;
ALTER INDEX analytics.idx_mv_hr_review_v2_work_date RENAME TO idx_mv_hr_review_work_date;
ALTER INDEX analytics.idx_mv_hr_review_v2_carrier_date RENAME TO idx_mv_hr_review_carrier_date;

-- ---------------------------------------------------------------------------
-- 3) v_daily_report_approvals: same narrowing for the DR Approval badge.
--    Anchored replace on pg_get_viewdef (the 215 §3 idiom): the view is ~6k
--    chars over base tables, so re-pasting it by hand is exactly the escaping
--    hazard migration 200 warned about. Anchor text verified against the live
--    definition 2026-08-11.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  def    text;
  anchor text := $a$COALESCE(((b.clock_in_late_minutes > 30) AND (b.task_status IS DISTINCT FROM 'cancelled'::text) AND (b.work_dow <> ALL (ARRAY[0, 6]))), false) AS is_tardy$a$;
  repl   text := $r$COALESCE(((b.clock_in_late_minutes > 30) AND (b.task_status IS DISTINCT FROM 'cancelled'::text) AND (b.work_dow <> ALL (ARRAY[0, 6])) AND (NOT EXISTS ( SELECT 1 FROM data_staging.stg_daily_report_hours h WHERE ((h.task_did = b.task_did) AND (h.work_description ~* '^\s*007\s*-?\s*UT\M'::text))))), false) AS is_tardy$r$;
BEGIN
  def := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass)), ';');

  IF position('has_undertime' IN def) > 0 OR position('007' IN def) > 0 THEN
    RAISE NOTICE '230: v_daily_report_approvals already undertime-aware; skipping.';
    RETURN;
  END IF;
  IF position(anchor IN def) = 0 THEN
    RAISE EXCEPTION '230: is_tardy anchor not found in v_daily_report_approvals, aborting.';
  END IF;

  EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || replace(def, anchor, repl);
END $$;

-- ---------------------------------------------------------------------------
-- 4) PostgREST must re-read the schema before the app can select has_undertime.
-- ---------------------------------------------------------------------------
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- ROLLBACK (behavioral, run in this order; NEVER DROP v_hr_report_review):
--   Keep the has_undertime column (CREATE OR REPLACE VIEW cannot drop it, and
--   an unused MV column is harmless) and revert only the verdicts:
--   1. CREATE OR REPLACE VIEW analytics.v_hr_report_review with THIS file's
--      step-2 body minus "AND NOT m.has_undertime" (has_undertime column
--      stays).
--   2. Re-run step 3 of THIS file with anchor and repl swapped to restore
--      v_daily_report_approvals.
--   3. NOTIFY pgrst, 'reload schema';
-- ---------------------------------------------------------------------------
