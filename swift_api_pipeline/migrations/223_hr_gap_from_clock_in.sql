-- 223: count the wait from clock-in to the first task as a timer gap
-- (Jamil 2026-08-05, reviewing PR #66 on localhost: "after he time in he didnt
-- work for 35 minutes, that should be considered as gap").
--
-- Case that motivated it: Joshua Jusay 2026-08-04 — clock-in 19:24:51 PHT,
-- first timer 19:59:40 PHT (34.8 idle minutes, uncounted), plus one counted
-- 79.6m gap (01:44->03:03). Expected after: 2 gaps / 114.4m.
--
-- Semantics change: the gap window now starts at CLOCK-IN, not at the first
-- task. long_gap_stats measures a leading gap from p_anchor to the first
-- participating block (COALESCE(lag(be), p_anchor)); the MV's gap lateral
-- passes r.first_clock_in as the anchor instead of fts.first_task_start.
-- Composition with 222's rules is natural:
--   * a timer already running at clock-in (straddler) gives a NEGATIVE leading
--     gap -> fails the 21m threshold -> no false gap;
--   * the leading gap is clipped at the cap like every other gap;
--   * participating blocks are unchanged (any merged block ending after
--     clock-in necessarily also ends after first_task_start, proven from the
--     fts min-start definition), so ONLY the leading gap is new;
--   * blank rules unchanged: the lateral still gates on fts.first_task_start
--     IS NOT NULL (all-timers-before-clock-in rows stay blank rather than
--     showing a misleading 0) and on usable stated hours; open-timer rows
--     still blank via the CASE.
-- A row whose first participating block starts after the cap now counts the
-- whole declared window as one gap (clock-in -> cap): the declared window had
-- zero timer coverage, which is exactly what the column is for.
--
-- Mechanics: CREATE OR REPLACE of the 5-arg long_gap_stats (same signature,
-- no overload dance), then the 170/222-pattern swap of mv_hr_report_review
-- only (rollup untouched). v_hr_report_review repointed with the identical
-- column list (built from the catalog, NEVER dropped). RPCs untouched.
--
-- Verification (run after): baseline 2026-08-05 pre-223 was rows 26,474 /
-- gap rows 11,004 / gaps 15,353 / 1,081,698.9 gap minutes / 6,216 high-idle
-- rows. Expect: same row count, same-or-equal gap rows, gaps and minutes UP,
-- Jusay 211201 2026-08-04 = 2 / 114.4.
--
-- Rollback at the bottom. NEVER DROP analytics.v_hr_report_review.

SET LOCAL lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0) Preflight: 222 must be applied; abort if 223 already is.
-- ---------------------------------------------------------------------------
DO $$
DECLARE cur text;
BEGIN
  IF to_regprocedure('analytics.long_gap_stats(timestamptz[],timestamptz[],timestamptz,numeric,timestamptz)') IS NULL THEN
    RAISE EXCEPTION '223: 5-arg long_gap_stats missing; apply 222 first';
  END IF;
  cur := pg_get_viewdef('analytics.mv_hr_report_review'::regclass);
  -- non-pretty viewdef renders the literal as (21)::numeric
  IF cur NOT LIKE '%fts.first_task_start, (21)::numeric%' THEN
    RAISE EXCEPTION '223: gap lateral does not anchor at first_task_start; drifted or already applied';
  END IF;
  IF to_regclass('analytics.mv_hr_report_review_v2') IS NOT NULL THEN
    RAISE EXCEPTION '223: mv_hr_report_review_v2 already exists; clean up before re-running';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) Gap policy: leading gap from the anchor. Only change vs 222 is
--    COALESCE(lag(b.be) ..., p_anchor); everything else verbatim. Still no
--    SET search_path (proconfig would block inlining into the MV build).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION analytics.long_gap_stats(
  p_block_starts timestamptz[],
  p_block_ends   timestamptz[],
  p_anchor       timestamptz,
  p_threshold_minutes numeric DEFAULT 21,
  p_cap          timestamptz DEFAULT NULL
)
RETURNS TABLE (long_gap_count bigint, long_gap_minutes numeric)
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT
    count(*) FILTER (WHERE g.gap_min >= p_threshold_minutes) AS long_gap_count,
    round(COALESCE(sum(g.gap_min) FILTER (WHERE g.gap_min >= p_threshold_minutes), 0)::numeric, 1) AS long_gap_minutes
  FROM (
    SELECT EXTRACT(epoch FROM LEAST(b.bs, COALESCE(p_cap, b.bs))
                   - COALESCE(lag(b.be) OVER (ORDER BY b.bs), p_anchor)) / 60.0 AS gap_min
    FROM unnest(p_block_starts, p_block_ends) AS b(bs, be)
    WHERE p_anchor IS NOT NULL AND b.be > p_anchor
  ) g
  WHERE g.gap_min IS NOT NULL
$function$;

-- ---------------------------------------------------------------------------
-- 2) New review MV: body = live pg_get_viewdef captured 2026-08-05 (post-222),
--    verbatim except the gap lateral's anchor argument (r.first_clock_in).
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
    ow.late_task_count
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
-- 3) Repoint the serving view, drop the old MV, rename v2 into place. The
--    view is NOT a thin passthrough: it overlays the 215 cancelled-DR
--    suppression on is_late_filing and computes is_tardy (38 cols vs the MV's
--    37) — body below is the live pg_get_viewdef captured 2026-08-05,
--    verbatim except FROM ..._v2. (A first apply attempt that regenerated the
--    column list from the MV failed on exactly this: "cannot drop columns
--    from view".) The refresh helper references the MV by name, so the rename
--    keeps refresh_hr_report_review_safe() working unchanged.
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
    COALESCE(m.clock_in_late_minutes > 30 AND m.task_status IS DISTINCT FROM 'cancelled'::text AND (m.work_dow <> ALL (ARRAY[0, 6])), false) AS is_tardy,
    m.first_task_start,
    m.last_task_end,
    m.long_gap_count,
    m.long_gap_minutes,
    m.early_task_count,
    m.late_task_count
   FROM analytics.mv_hr_report_review_v2 m;

DROP MATERIALIZED VIEW analytics.mv_hr_report_review;

ALTER MATERIALIZED VIEW analytics.mv_hr_report_review_v2 RENAME TO mv_hr_report_review;
ALTER INDEX analytics.mv_hr_report_review_v2_pk RENAME TO mv_hr_report_review_pk;
ALTER INDEX analytics.idx_mv_hr_review_v2_work_date RENAME TO idx_mv_hr_review_work_date;
ALTER INDEX analytics.idx_mv_hr_review_v2_carrier_date RENAME TO idx_mv_hr_review_carrier_date;

NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- Rollback (run in this order; NEVER DROP v_hr_report_review):
--   1. CREATE OR REPLACE FUNCTION analytics.long_gap_stats(... 5-arg ...) with
--      the 222 body (lag(b.be) with NO COALESCE fallback to p_anchor).
--   2. Re-run steps 2-3 of THIS file but with the gap lateral anchored at
--      fts.first_task_start (the 222 body) to swap the MV back.
-- ---------------------------------------------------------------------------
