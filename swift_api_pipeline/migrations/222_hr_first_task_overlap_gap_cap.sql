-- 222: First-task overlap rule + timer-gap cap at declared hours (Jamil 2026-08-05).
-- Spec: ontel-people/docs/superpowers/specs/2026-08-05-first-task-overlap-gap-cap-design.md
--
-- Definitions (user-confirmed):
--   first_task_start  min timer start among entries whose END is NULL (open) or
--                     > first_clock_in: the first task that touches or follows the
--                     time-in (a task started 8pm running past a 9pm time-in shows
--                     8pm). NULL without a clock-in. Was: min start AT/AFTER clock-in.
--   long_gap_*        as migration 217 (21m+ gaps between merged blocks from the
--                     first block ending after the anchor) but each gap is CLIPPED
--                     at first_clock_in + raw stated hours; the 21m threshold
--                     applies to the clipped portion. NULL while a timer is open,
--                     without a clock-in, or (NEW) when total_hours is null/zero.
--
-- Mechanics: 170-pattern MV swap for both MVs (MV bodies cannot be edited in
-- place). v_hr_report_review is repointed via CREATE OR REPLACE with the SAME 38
-- columns (215 body + 217 append; NEVER dropped). RPCs untouched (221 bodies
-- reference only unchanged column names). long_gap_stats gains p_cap as a 5th
-- defaulted arg: the new overload is created first (the old review MV pins the
-- 4-arg OID), the 4-arg version is dropped after the old MV drops.
--
-- Rollback is at the bottom. NEVER DROP analytics.v_hr_report_review.

-- Same deadlock rationale as 217: fail fast on lock contention with the */5
-- pg_cron refresh instead of deadlocking; on lock_timeout, re-apply in a quiet
-- window.
SET LOCAL lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0) Preflight: abort if drifted or already applied.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  PERFORM 'analytics.long_gap_stats(timestamptz[],timestamptz[],timestamptz,numeric)'::regprocedure;
  IF to_regprocedure('analytics.long_gap_stats(timestamptz[],timestamptz[],timestamptz,numeric,timestamptz)') IS NOT NULL THEN
    RAISE EXCEPTION '222: 5-arg long_gap_stats already exists; migration appears applied';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_attribute
    WHERE attrelid = 'analytics.mv_timer_day_rollup'::regclass
      AND attname = 'end_times' AND NOT attisdropped
  ) THEN
    RAISE EXCEPTION '222: mv_timer_day_rollup already has end_times; migration appears applied';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) New 5-arg gap policy function. p_cap clips each between-block gap: the
--    gap ends at LEAST(next_block_start, p_cap); the threshold applies to the
--    clipped portion, so a gap whose in-window part is under the threshold no
--    longer counts, and gaps at/after the cap contribute nothing (clipped
--    length <= 0 fails the >= threshold filter). NULL cap = uncapped (217
--    behavior). Deliberately no SET search_path: a proconfig entry would block
--    inlining into the MV build (same note as 217).
-- ---------------------------------------------------------------------------
CREATE FUNCTION analytics.long_gap_stats(
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
    SELECT EXTRACT(epoch FROM LEAST(b.bs, COALESCE(p_cap, b.bs)) - lag(b.be) OVER (ORDER BY b.bs)) / 60.0 AS gap_min
    FROM unnest(p_block_starts, p_block_ends) AS b(bs, be)
    WHERE p_anchor IS NOT NULL AND b.be > p_anchor
  ) g
  WHERE g.gap_min IS NOT NULL
$function$;

-- ---------------------------------------------------------------------------
-- 2) Rebuild the timer day rollup: body = migration 217 with end_times added.
--    start_times and end_times MUST stay aligned: both aggregates share the
--    identical ORDER BY s, e (e sorts open-entry NULLs last within a tie,
--    deterministically, in both arrays).
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW analytics.mv_timer_day_rollup_v2 AS
WITH iv AS (
  SELECT lower(user_email) AS user_email,
         (start_time AT TIME ZONE 'America/New_York')::date AS work_day,
         start_time AS s, end_time AS e
  FROM data_staging.stg_timer_activities_clean
  WHERE user_email IS NOT NULL AND start_time IS NOT NULL
), resolved AS (
  SELECT iv.*,
         COALESCE(a.emp_id, 'email:' || iv.user_email) AS person_key,
         a.emp_id
  FROM iv
  LEFT JOIN LATERAL (
    SELECT ea.emp_id FROM reference.ref_employee_emails ea
    WHERE ea.email = iv.user_email
    ORDER BY ea.last_seen DESC LIMIT 1
  ) a ON true
), closed AS (
  SELECT * FROM resolved WHERE e IS NOT NULL AND e > s
), marked AS (
  SELECT person_key, work_day, s, e,
         CASE WHEN s > max(e) OVER (PARTITION BY person_key, work_day
              ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
              THEN 1 ELSE 0 END AS nb
  FROM closed
), blocks AS (
  SELECT person_key, work_day, s, e,
         sum(nb) OVER (PARTITION BY person_key, work_day ORDER BY s, e) AS blk
  FROM marked
), merged AS (
  SELECT person_key, work_day, blk, min(s) AS bs, max(e) AS be
  FROM blocks GROUP BY person_key, work_day, blk
), uni AS (
  SELECT person_key, work_day,
         round((sum(EXTRACT(epoch FROM be - bs)) / 60.0)::numeric, 1) AS union_min,
         max(be)                    AS last_end,
         array_agg(bs ORDER BY bs)  AS block_starts,
         array_agg(be ORDER BY bs)  AS block_ends
  FROM merged GROUP BY person_key, work_day
), counts AS (
  SELECT person_key, max(emp_id) AS emp_id, work_day,
         count(*) AS entry_count,
         count(*) FILTER (WHERE e IS NULL) AS open_count,
         min(s) AS first_start,
         -- every start (open included; starts are final at start time)
         array_agg(s ORDER BY s, e) AS start_times,
         -- 222: per-entry ends aligned with start_times (NULL = still open)
         array_agg(e ORDER BY s, e) AS end_times
  FROM resolved GROUP BY person_key, work_day
)
SELECT c.person_key, c.emp_id, c.work_day, COALESCE(u.union_min, 0) AS union_min,
       c.entry_count, c.open_count, c.first_start,
       u.last_end, c.start_times, c.end_times, u.block_starts, u.block_ends
FROM counts c LEFT JOIN uni u USING (person_key, work_day)
WITH DATA;

CREATE UNIQUE INDEX mv_timer_day_rollup_v2_pk
  ON analytics.mv_timer_day_rollup_v2 (person_key, work_day);

-- ---------------------------------------------------------------------------
-- 3) Repoint the rollup's other dependents onto _v2 BEFORE the old MV drops
--    (217 idiom: take the LIVE viewdef, swap the relation name).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  vw   text;
  body text;
BEGIN
  FOREACH vw IN ARRAY ARRAY['v_daily_report_approvals', 'v_unmatched_timer_emails'] LOOP
    body := rtrim(btrim(pg_get_viewdef(('analytics.' || vw)::regclass, true)), ';');
    IF position('mv_timer_day_rollup_v2' IN body) > 0 THEN
      RAISE NOTICE '222: analytics.% already on _v2; skipping.', vw;
      CONTINUE;
    END IF;
    IF position('mv_timer_day_rollup' IN body) = 0 THEN
      RAISE NOTICE '222: analytics.% does not reference the rollup; skipping.', vw;
      CONTINUE;
    END IF;
    EXECUTE 'CREATE OR REPLACE VIEW analytics.' || vw || ' AS '
      || replace(body, 'mv_timer_day_rollup', 'mv_timer_day_rollup_v2');
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 4) Rebuild the review MV: body = migration 217 with (a) the fts lateral on
--    entry ENDS instead of starts, (b) the gap lateral passing the cap and
--    gated on usable stated hours. Everything else identical.
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
    CASE WHEN COALESCE(tm.open_count, 0::bigint) = 0 THEN gap.long_gap_count ELSE NULL::bigint END AS long_gap_count,
    CASE WHEN COALESCE(tm.open_count, 0::bigint) = 0 THEN gap.long_gap_minutes ELSE NULL::numeric END AS long_gap_minutes,
    ow.early_task_count,
    ow.late_task_count
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup_v2 tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup_v2 m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true
     -- 222: first task that touches or follows clock-in (entry END after
     -- clock-in; open entries qualify). Was: min start at/after clock-in.
     LEFT JOIN LATERAL (
        SELECT min(x.st) AS first_task_start
        FROM unnest(tm.start_times, tm.end_times) AS x(st, en)
        WHERE x.en IS NULL OR x.en > r.first_clock_in
     ) fts ON tm.person_key IS NOT NULL AND r.first_clock_in IS NOT NULL
     -- 222: cap at clock-in + raw stated hours; no usable stated hours = no
     -- verdict (join condition false -> NULL columns).
     LEFT JOIN LATERAL (
        SELECT g.long_gap_count, g.long_gap_minutes
        FROM analytics.long_gap_stats(
          tm.block_starts, tm.block_ends, fts.first_task_start, 21,
          r.first_clock_in + make_interval(secs => (b.total_hours * 3600)::double precision)
        ) g
     ) gap ON tm.person_key IS NOT NULL AND fts.first_task_start IS NOT NULL
              AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
     LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE st < r.first_clock_in)  AS early_task_count,
               count(*) FILTER (WHERE st > r.first_clock_in
                 + make_interval(secs => (b.total_hours * 3600)::double precision)) AS late_task_count
        FROM unnest(tm.start_times) AS st
     ) ow ON tm.person_key IS NOT NULL AND r.first_clock_in IS NOT NULL
             AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
WITH DATA;

CREATE UNIQUE INDEX mv_hr_report_review_v2_pk
  ON analytics.mv_hr_report_review_v2 (task_did);
CREATE INDEX idx_mv_hr_review_v2_work_date
  ON analytics.mv_hr_report_review_v2 (work_date, task_did);
CREATE INDEX idx_mv_hr_review_v2_carrier_date
  ON analytics.mv_hr_report_review_v2 (carrier_group, work_date);

-- ---------------------------------------------------------------------------
-- 5) Repoint the serving view onto _v2. Body identical to migration 217 step 5
--    (same 38 columns, same order; append-only contract preserved, nothing
--    appended this time). NEVER DROP this view.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
SELECT
  m.emp_id,
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
  COALESCE(m.is_late_filing AND m.task_status IS DISTINCT FROM 'cancelled', false) AS is_late_filing,
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
  COALESCE(
    m.clock_in_late_minutes > 30
      AND m.task_status IS DISTINCT FROM 'cancelled'
      AND m.work_dow NOT IN (0, 6)
  , false) AS is_tardy,
  m.first_task_start,
  m.last_task_end,
  m.long_gap_count,
  m.long_gap_minutes,
  m.early_task_count,
  m.late_task_count
FROM analytics.mv_hr_report_review_v2 m;

COMMENT ON VIEW analytics.v_hr_report_review IS
  'HR daily-report review serving view over mv_hr_report_review. is_tardy per migration 215. first_task_start = first timer overlapping or following clock-in (222). long_gap_* = 21m threshold, clipped at clock-in + raw stated hours; NULL while a timer is open, without a clock-in, or without usable stated hours (222). early/late_task_count per migration 217.';

-- ---------------------------------------------------------------------------
-- 6) Swap: verify no unexpected dependents, drop old MVs, take over the
--    canonical names, then retire the 4-arg gap function (its only dependent
--    was the old review MV).
-- ---------------------------------------------------------------------------
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n
  FROM pg_depend d
  JOIN pg_rewrite rw ON rw.oid = d.objid
  JOIN pg_class dep ON dep.oid = rw.ev_class
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refobjid IN ('analytics.mv_hr_report_review'::regclass, 'analytics.mv_timer_day_rollup'::regclass)
    AND rw.ev_class <> d.refobjid
    AND (dep.relnamespace::regnamespace::text, dep.relname) NOT IN (('analytics','mv_hr_report_review'), ('analytics','mv_timer_day_rollup'));
  IF n > 0 THEN
    RAISE EXCEPTION '222: old MVs still have % dependent view(s); aborting before DROP', n;
  END IF;
END $$;

DROP MATERIALIZED VIEW analytics.mv_hr_report_review;
DROP MATERIALIZED VIEW analytics.mv_timer_day_rollup;

ALTER MATERIALIZED VIEW analytics.mv_hr_report_review_v2 RENAME TO mv_hr_report_review;
ALTER INDEX analytics.mv_hr_report_review_v2_pk RENAME TO mv_hr_report_review_pk;
ALTER INDEX analytics.idx_mv_hr_review_v2_work_date RENAME TO idx_mv_hr_review_work_date;
ALTER INDEX analytics.idx_mv_hr_review_v2_carrier_date RENAME TO idx_mv_hr_review_carrier_date;

ALTER MATERIALIZED VIEW analytics.mv_timer_day_rollup_v2 RENAME TO mv_timer_day_rollup;
ALTER INDEX analytics.mv_timer_day_rollup_v2_pk RENAME TO mv_timer_day_rollup_pk;

-- refresh_*_safe() are SECURITY DEFINER owned by postgres (verified 2026-08-03)
ALTER MATERIALIZED VIEW analytics.mv_hr_report_review OWNER TO postgres;
ALTER MATERIALIZED VIEW analytics.mv_timer_day_rollup OWNER TO postgres;

DROP FUNCTION analytics.long_gap_stats(timestamptz[], timestamptz[], timestamptz, numeric);

-- ---------------------------------------------------------------------------
-- 7) Access + semantic metadata (217 pattern: GRANT/REVOKE + guarded UPDATEs).
-- ---------------------------------------------------------------------------
GRANT SELECT ON analytics.mv_hr_report_review TO service_role;
REVOKE ALL ON analytics.mv_hr_report_review FROM anon, authenticated;
GRANT SELECT ON analytics.mv_timer_day_rollup TO service_role;
REVOKE ALL ON analytics.mv_timer_day_rollup FROM anon, authenticated;

DO $$
DECLARE rows_affected int;
BEGIN
  UPDATE agent.schema_metadata
  SET description = 'Per (person_key, ET work day) timer rollup: union_min (merged closed intervals), entry_count, open_count, first_start, last_end (max closed end), start_times/end_times (all entries incl. open as parallel ordered arrays; end NULL = still open, 222), block_starts/block_ends (merged disjoint blocks as parallel arrays). person_key = emp_id when resolvable else ''email:<address>''. Refreshed every 10 min by pg_cron.'
  WHERE schema_name = 'analytics' AND table_name = 'mv_timer_day_rollup' AND column_name IS NULL;
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected = 0 THEN
    RAISE EXCEPTION '222: schema_metadata row missing for analytics.mv_timer_day_rollup';
  END IF;

  UPDATE agent.schema_metadata
  SET description = 'Materialized snapshot of the HR daily-report review layer, one row per report task (task_did grain). 37 columns. first_task_start = first timer overlapping or following clock-in (entry end after clock-in, open entries qualify; 222). long_gap_count/long_gap_minutes = 21m+ gaps between merged blocks from the first after-anchor block, each gap clipped at clock-in + raw stated hours; NULL when a timer is open, no clock-in, or stated hours null/zero (222). early/late_task_count = entry starts outside clock-in + raw stated hours. v_hr_report_review is the serving pass-through.',
      related_tables = ARRAY['analytics.v_hr_report_review','analytics.v_daily_report_approvals','analytics.mv_daily_report_task_rollup','analytics.mv_timer_day_rollup','data_staging.stg_daily_reports','analytics.long_gap_stats']
  WHERE schema_name = 'analytics' AND table_name = 'mv_hr_report_review' AND column_name IS NULL;
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected = 0 THEN
    RAISE EXCEPTION '222: schema_metadata row missing for analytics.mv_hr_report_review';
  END IF;
END $$;

NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- ROLLBACK (view columns unchanged, so no app rollback needed first):
--   1. Re-run steps 1-6 of migration 217 (its exact MV bodies and 4-arg
--      long_gap_stats), i.e. restore the at/after-clock-in fts lateral, the
--      uncapped gap lateral without the stated-hours join condition, and the
--      rollup without end_times; repoint the serving view the same way.
--   2. DROP FUNCTION IF EXISTS analytics.long_gap_stats(timestamptz[], timestamptz[], timestamptz, numeric, timestamptz);
--   3. Revert the two agent.schema_metadata descriptions to the 217 wording.
--   4. NOTIFY pgrst, 'reload schema';
-- ---------------------------------------------------------------------------
