-- 217: First/last task, 21m+ gap stats, and outside-window task counts for the
-- DR Monitoring table (Jamil 2026-08-03).
-- APPLIED to prod 2026-08-03 ~11:03p ET via MCP apply_migration. Verified same
-- night: row parity exact (rollup 54,963 = 54,963; review 26,352 = 26,352);
-- NULL-gate violations 0 (open-timer, no-clock-in, anchor); long_gap_stats
-- fixture (1, 35.0); RPC flag counts = direct view counts (long_gaps 6,281;
-- outside_window 3,928); first post-swap cron tick (jobid 9, 11:05p ET)
-- succeeded in 3.5s vs 2.8-5.2s pre-217 baseline.
-- Spec: ontel-people/docs/superpowers/specs/2026-08-03-dr-monitoring-column-picker-design.md
--
-- Definitions (user-confirmed):
--   first_task_start  min timer start AT/AFTER first_clock_in (open entries count;
--                     a start is final the moment the timer starts). NULL without a clock-in.
--   last_task_end     max CLOSED timer end of the ET work day, any task.
--   long_gap_*        gaps >= 21 min between consecutive MERGED work blocks, counted
--                     from the first block that ends after first_task_start through the
--                     last block. Leading/trailing stretches never count. NULL while a
--                     timer is open (timeline not final; same convention as variance)
--                     or without a clock-in (no anchor).
--   early/late_task_count  timer entries whose START is < first_clock_in / > first_clock_in
--                     + stated_hours (RAW total_hours incl. break). NULL when there is no
--                     window (no clock-in, or total_hours null/zero): unknown, not 0.
--
-- Both MVs rebuild via the migration-170 swap pattern (MVs cannot add columns).
-- v_hr_report_review gains 6 APPENDED columns via CREATE OR REPLACE; the view is
-- NEVER dropped (hr_review_page RETURNS SETOF it). The two list RPCs are
-- DROP+CREATEd with full bodies = migration 199 + the 215 is_tardy predicate +
-- this migration's sorts/flags (a preflight aborts if the live defs drifted).
-- Refresh wiring is untouched: refresh_* functions and cron reference the
-- canonical names as text.
--
-- Rollback is at the bottom. NEVER DROP analytics.v_hr_report_review.

-- The apply channel (MCP apply_migration / migrate_cloud.py) wraps each
-- migration in its own transaction, matching neighbors 199/207/215 which
-- carry no explicit BEGIN/COMMIT. This migration holds AccessExclusiveLock on
-- v_daily_report_approvals (step 3) while step 6 later needs a lock on
-- mv_hr_report_review; pg_cron jobid 9 (*/5 refresh) can want the opposite
-- lock order -> deadlock window. Pausing cron INSIDE this transaction would
-- be a no-op (cron.job changes only become visible at commit), so we fail
-- fast instead: lock_timeout aborts cleanly rather than deadlocking. On a
-- lock_timeout error, simply re-apply during a quiet 5-min-cron window.
SET LOCAL lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0) Preflight: the RPC bodies we re-create below assume 199 + 215. Abort if
--    the live functions drifted somewhere else.
-- ---------------------------------------------------------------------------
DO $$
DECLARE def text;
BEGIN
  -- Resolved by signature, not name: avoids ambiguity if an overloaded
  -- hr_review_page ever exists mid-migration.
  def := pg_get_functiondef('analytics.hr_review_page(text,text,text,date,date,text[],text,text,integer,integer,text[],text[],integer[],integer,integer)'::regprocedure);
  IF def IS NULL THEN RAISE EXCEPTION '217: hr_review_page missing'; END IF;
  IF position('v.is_tardy' IN def) = 0 THEN
    RAISE EXCEPTION '217: hr_review_page does not carry the 215 is_tardy predicate; re-check drift before applying';
  END IF;
  IF position('long_gap_minutes' IN def) > 0 THEN
    RAISE EXCEPTION '217: hr_review_page already has 217 changes; migration appears applied';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) Gap policy as a callable function so ontel-people's sql-sync drift test
--    can fixture it (same pattern as analytics.approval_wait_days, mig 199).
--    Blocks are the merged disjoint intervals (parallel ordered arrays).
--    Gaps count between consecutive blocks that END after p_anchor; the first
--    participating block contributes no leading gap (lag is NULL).
-- ---------------------------------------------------------------------------
-- Deliberately no SET search_path (unlike approval_wait_days, mig 199): a
-- proconfig entry on this function would block SRF inlining into the MV
-- build below, forcing per-row function calls instead of a flattened join.
CREATE OR REPLACE FUNCTION analytics.long_gap_stats(
  p_block_starts timestamptz[],
  p_block_ends   timestamptz[],
  p_anchor       timestamptz,
  p_threshold_minutes numeric DEFAULT 21
)
RETURNS TABLE (long_gap_count bigint, long_gap_minutes numeric)
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT
    count(*) FILTER (WHERE g.gap_min >= p_threshold_minutes) AS long_gap_count,
    round(COALESCE(sum(g.gap_min) FILTER (WHERE g.gap_min >= p_threshold_minutes), 0)::numeric, 1) AS long_gap_minutes
  FROM (
    SELECT EXTRACT(epoch FROM b.bs - lag(b.be) OVER (ORDER BY b.bs)) / 60.0 AS gap_min
    FROM unnest(p_block_starts, p_block_ends) AS b(bs, be)
    WHERE p_anchor IS NOT NULL AND b.be > p_anchor
  ) g
  WHERE g.gap_min IS NOT NULL
$function$;

-- ---------------------------------------------------------------------------
-- 2) Rebuild the timer day rollup with last_end + the raw arrays the review MV
--    needs. Body = migration 170 + the marked additions; person resolution and
--    ET work-day attribution stay single-sourced here.
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
         -- 217: last completed end + merged blocks as parallel ordered arrays
         max(be)                    AS last_end,
         array_agg(bs ORDER BY bs)  AS block_starts,
         array_agg(be ORDER BY bs)  AS block_ends
  FROM merged GROUP BY person_key, work_day
), counts AS (
  SELECT person_key, max(emp_id) AS emp_id, work_day,
         count(*) AS entry_count,
         count(*) FILTER (WHERE e IS NULL) AS open_count,
         min(s) AS first_start,
         -- 217: every start (open included; starts are final at start time)
         array_agg(s ORDER BY s) AS start_times
  FROM resolved GROUP BY person_key, work_day
)
SELECT c.person_key, c.emp_id, c.work_day, COALESCE(u.union_min, 0) AS union_min,
       c.entry_count, c.open_count, c.first_start,
       u.last_end, c.start_times, u.block_starts, u.block_ends
FROM counts c LEFT JOIN uni u USING (person_key, work_day)
WITH DATA;

CREATE UNIQUE INDEX mv_timer_day_rollup_v2_pk
  ON analytics.mv_timer_day_rollup_v2 (person_key, work_day);

-- ---------------------------------------------------------------------------
-- 3) Repoint the rollup's other dependents onto _v2 BEFORE the old MV drops.
--    v_daily_report_approvals was wrapped dynamically by migration 215, so we
--    never re-paste a stale body: take the LIVE viewdef and swap the relation
--    name. Idempotence guard: skip when the body already references _v2.
--    v_unmatched_timer_emails (mig 170 s4) gets the same treatment.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  vw   text;
  body text;
BEGIN
  FOREACH vw IN ARRAY ARRAY['v_daily_report_approvals', 'v_unmatched_timer_emails'] LOOP
    body := rtrim(btrim(pg_get_viewdef(('analytics.' || vw)::regclass, true)), ';');
    IF position('mv_timer_day_rollup_v2' IN body) > 0 THEN
      RAISE NOTICE '217: analytics.% already on _v2; skipping.', vw;
      CONTINUE;
    END IF;
    IF position('mv_timer_day_rollup' IN body) = 0 THEN
      RAISE NOTICE '217: analytics.% does not reference the rollup; skipping.', vw;
      CONTINUE;
    END IF;
    EXECUTE 'CREATE OR REPLACE VIEW analytics.' || vw || ' AS '
      || replace(body, 'mv_timer_day_rollup', 'mv_timer_day_rollup_v2');
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 4) Rebuild the review MV: body = migration 207 (the MV was untouched by 215,
--    which only changed the VIEW) with tm/th repointed to _v2 and the six new
--    columns computed via laterals, then appended LAST.
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
    -- 217 appended columns -------------------------------------------------
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
     LEFT JOIN LATERAL (
        SELECT min(st) AS first_task_start
        FROM unnest(tm.start_times) AS st
        WHERE st >= r.first_clock_in
     ) fts ON tm.person_key IS NOT NULL AND r.first_clock_in IS NOT NULL
     LEFT JOIN LATERAL (
        SELECT g.long_gap_count, g.long_gap_minutes
        FROM analytics.long_gap_stats(tm.block_starts, tm.block_ends, fts.first_task_start) g
     ) gap ON tm.person_key IS NOT NULL AND fts.first_task_start IS NOT NULL
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
-- 5) Serving view: migration 215's body (32 columns: 31 from 207 + is_tardy)
--    over the new MV, with the 6 new columns APPENDED. Append-only, never
--    reorder.
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
  -- 217 appended columns
  m.first_task_start,
  m.last_task_end,
  m.long_gap_count,
  m.long_gap_minutes,
  m.early_task_count,
  m.late_task_count
FROM analytics.mv_hr_report_review_v2 m;

COMMENT ON VIEW analytics.v_hr_report_review IS
  'HR daily-report review serving view over mv_hr_report_review. is_tardy per migration 215; first/last task, long_gap_* (21m threshold, NULL while a timer is open or without clock-in), early/late_task_count (outside clock-in + raw stated hours; NULL without a window) per migration 217.';

-- ---------------------------------------------------------------------------
-- 6) Drop the old MVs (no CASCADE; every dependent was repointed above), then
--    take over the canonical names. Refresh functions/cron reference names as
--    text, so the swap needs zero wiring changes.
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
    RAISE EXCEPTION '217: old MVs still have % dependent view(s); aborting before DROP', n;
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

-- REFRESH requires ownership; refresh_*_safe() are SECURITY DEFINER owned by
-- postgres (live owners verified postgres 2026-08-03).
ALTER MATERIALIZED VIEW analytics.mv_hr_report_review OWNER TO postgres;
ALTER MATERIALIZED VIEW analytics.mv_timer_day_rollup OWNER TO postgres;

-- ---------------------------------------------------------------------------
-- 7) List RPCs: full bodies = migration 199 + the 215 is_tardy predicate
--    (asserted by the step-0 preflight) + two flag predicates + three sort
--    keys. Signatures unchanged; DROP keeps the diff explicit (199 idiom).
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS analytics.hr_review_page(text, text, text, date, date, text[], text, text, integer, integer, text[], text[], integer[], integer, integer);
DROP FUNCTION IF EXISTS analytics.hr_review_count(text, text, text, date, date, text[], text[], text[], integer[], integer, integer);

CREATE FUNCTION analytics.hr_review_page(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_sort_key text DEFAULT NULL::text,
  p_sort_dir text DEFAULT 'desc'::text,
  p_offset integer DEFAULT 0,
  p_limit integer DEFAULT 100,
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer
)
 RETURNS SETOF analytics.v_hr_report_review
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select v.*
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
  order by
    case when p_sort_key = 'employee_name' and p_sort_dir = 'asc'  then v.employee_name end asc  nulls last,
    case when p_sort_key = 'employee_name' and p_sort_dir = 'desc' then v.employee_name end desc nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'asc'  then v.emp_id        end asc  nulls last,
    case when p_sort_key = 'emp_id'        and p_sort_dir = 'desc' then v.emp_id        end desc nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'asc'  then v.clock_in_et   end asc  nulls last,
    case when p_sort_key = 'clock_in_et'   and p_sort_dir = 'desc' then v.clock_in_et   end desc nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'asc'  then v.first_task_start end asc  nulls last,
    case when p_sort_key = 'first_task_start' and p_sort_dir = 'desc' then v.first_task_start end desc nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'asc'  then v.last_task_end    end asc  nulls last,
    case when p_sort_key = 'last_task_end'    and p_sort_dir = 'desc' then v.last_task_end    end desc nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'asc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end asc  nulls last,
    case when p_sort_key = 'approval_wait' and p_sort_dir = 'desc'
      then analytics.approval_wait_days(v.work_date, v.task_status, v.submitted_on_et, v.approved_on_et) end desc nulls last,
    case when p_sort_dir = 'asc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end asc nulls last,
    case when p_sort_dir = 'desc' then
      case p_sort_key
        when 'filing_lag_hours' then v.filing_lag_hours
        when 'stated_hours'     then v.stated_hours_net
        when 'timed_hours'      then v.timed_hours
        when 'variance_hours'   then v.variance_hours
        when 'long_gap_minutes' then v.long_gap_minutes
      end
    end desc nulls last,
    case when p_sort_key = 'work_date' and p_sort_dir = 'asc' then v.work_date end asc,
    case when p_sort_key is distinct from 'work_date' or p_sort_dir <> 'asc' then v.work_date end desc,
    v.task_did asc
  offset greatest(coalesce(p_offset, 0), 0)
  limit  least(greatest(coalesce(p_limit, 100), 0), 1000)
$function$;

CREATE FUNCTION analytics.hr_review_count(
  p_search text DEFAULT NULL::text,
  p_carrier_group text DEFAULT NULL::text,
  p_status text DEFAULT NULL::text,
  p_date_from date DEFAULT NULL::date,
  p_date_to date DEFAULT NULL::date,
  p_flags text[] DEFAULT '{}'::text[],
  p_carrier_groups text[] DEFAULT NULL::text[],
  p_statuses text[] DEFAULT NULL::text[],
  p_dows integer[] DEFAULT NULL::integer[],
  p_untimed_min integer DEFAULT NULL::integer,
  p_untimed_max integer DEFAULT NULL::integer
)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  select count(*)
  from analytics.v_hr_report_review v
  where (p_carrier_group is null or v.carrier_group = p_carrier_group)
    and (p_carrier_groups is null or v.carrier_group = any(p_carrier_groups))
    and (p_status is null or v.task_status = p_status)
    and (p_statuses is null or v.task_status = any(p_statuses))
    and (p_search is null or p_search = ''
         or v.employee_name ilike '%' || p_search || '%'
         or v.emp_id ilike '%' || p_search || '%')
    and (p_date_from is null or v.work_date >= p_date_from)
    and (p_date_to is null or v.work_date <= p_date_to)
    and (p_dows is null or cardinality(p_dows) = 0 or v.work_dow = any(p_dows))
    and (p_untimed_min is null or (v.coverage_pct is not null and v.coverage_pct <= 100 - p_untimed_min))
    and (p_untimed_max is null or (v.coverage_pct is not null and v.coverage_pct >= 100 - p_untimed_max))
    and (
      coalesce(cardinality(p_flags), 0) = 0
      or ('late'         = any(p_flags) and v.is_late_filing)
      or ('missing'      = any(p_flags) and v.is_missing_report)
      or ('variance'     = any(p_flags) and v.coverage_pct <= 84)
      or ('variance_red' = any(p_flags) and v.coverage_pct <= 84)
      or ('open_timer'   = any(p_flags) and v.open_timer_count > 0)
      or ('no_time_in'   = any(p_flags) and not v.has_time_in and v.submitted_on_et is not null)
      or ('tardy'        = any(p_flags) and v.is_tardy)
      or ('no_clock_in'  = any(p_flags) and v.clock_in_et is null)
      or ('long_gaps'      = any(p_flags) and v.long_gap_minutes > 60)
      or ('outside_window' = any(p_flags) and (coalesce(v.early_task_count, 0) > 0 or coalesce(v.late_task_count, 0) > 0))
    )
$function$;

-- ---------------------------------------------------------------------------
-- 8) Access + semantic metadata: GRANT/REVOKE (207 pattern) plus guarded
--    UPDATEs of existing rows. Both rows already exist (INSERTed by
--    migrations 170 and 207 respectively), so this is UPDATE not INSERT; a
--    plain UPDATE that matched zero rows would silently no-op and leave the
--    metadata undocumenting the new 217 columns, so each is wrapped with a
--    GET DIAGNOSTICS check that aborts instead.
-- ---------------------------------------------------------------------------
GRANT SELECT ON analytics.mv_hr_report_review TO service_role;
REVOKE ALL ON analytics.mv_hr_report_review FROM anon, authenticated;
GRANT SELECT ON analytics.mv_timer_day_rollup TO service_role;
REVOKE ALL ON analytics.mv_timer_day_rollup FROM anon, authenticated;

DO $$
DECLARE rows_affected int;
BEGIN
  UPDATE agent.schema_metadata
  SET description = 'Per (person_key, ET work day) timer rollup: union_min (merged closed intervals), entry_count, open_count, first_start, last_end (max closed end, 217), start_times (all starts incl. open, 217), block_starts/block_ends (merged disjoint blocks as parallel arrays, 217). person_key = emp_id when resolvable else ''email:<address>''. Refreshed every 10 min by pg_cron.'
  WHERE schema_name = 'analytics' AND table_name = 'mv_timer_day_rollup' AND column_name IS NULL;
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected = 0 THEN
    RAISE EXCEPTION '217: schema_metadata row missing for analytics.mv_timer_day_rollup';
  END IF;

  UPDATE agent.schema_metadata
  SET description = 'Materialized snapshot of the HR daily-report review layer, one row per report task (task_did grain). 37 columns after migration 217: adds first_task_start (first start at/after clock-in), last_task_end, long_gap_count/long_gap_minutes (21m+ gaps between merged blocks from the first after-clock-in task; NULL when a timer is open or no clock-in), early/late_task_count (entry starts outside clock-in + raw stated hours; NULL without a window). v_hr_report_review is the serving pass-through.',
      related_tables = ARRAY['analytics.v_hr_report_review','analytics.v_daily_report_approvals','analytics.mv_daily_report_task_rollup','analytics.mv_timer_day_rollup','data_staging.stg_daily_reports','analytics.long_gap_stats']
  WHERE schema_name = 'analytics' AND table_name = 'mv_hr_report_review' AND column_name IS NULL;
  GET DIAGNOSTICS rows_affected = ROW_COUNT;
  IF rows_affected = 0 THEN
    RAISE EXCEPTION '217: schema_metadata row missing for analytics.mv_hr_report_review';
  END IF;
END $$;

NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- ROLLBACK (view columns can never be dropped without DROP VIEW, which is
-- forbidden; roll the app back FIRST if it already selects the new columns):
--   1. Re-run this file's steps 2-6 with the 170/207 MV bodies (no new
--      columns), then CREATE OR REPLACE v_hr_report_review with the 215 body
--      PLUS the six 217 columns as typed NULLs appended in the same order:
--        NULL::timestamptz AS first_task_start, NULL::timestamptz AS last_task_end,
--        NULL::bigint AS long_gap_count, NULL::numeric AS long_gap_minutes,
--        NULL::bigint AS early_task_count, NULL::bigint AS late_task_count
--   2. DROP+CREATE the two RPCs with the 199 bodies + the 215 tardy line
--      (i.e. this file's step 7 minus the long_gaps/outside_window predicates
--      and the three new sort branches).
--   3. DROP FUNCTION IF EXISTS analytics.long_gap_stats(timestamptz[], timestamptz[], timestamptz, numeric);
--   4. NOTIFY pgrst, 'reload schema';
-- ---------------------------------------------------------------------------
