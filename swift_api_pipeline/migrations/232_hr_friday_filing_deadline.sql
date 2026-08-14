-- 232: Friday work dates get a weekend extension on the filing deadline.
-- (Jamil 2026-08-13: "for dr filing for those friday we need to set the alarm
-- from 48hr to 60hr and if the dr is on friday it should be more than 61hr to
-- consider as late. for other day its normal 48hr notif and 49hr for late".)
--
-- Rule: when the DR's work_date falls on a Friday (extract(dow) = 5), the
-- filing deadline is clock-in + 61h instead of + 49h. Same boundary
-- convention as migration 179: the whole final hour still counts as on time,
-- late the instant the lag reaches 61:00:00 (49:00:00 for other days).
-- Rationale: Friday clock-in + 49h lands on Sunday; + 61h lands Monday
-- morning. The 48h->60h reminder ("alarm") half of the rule lives in
-- analytics.report_reminder_candidates (ontel-people migration 032); the
-- 24h nudge is unchanged.
--
-- Touched here (the only remaining hardcoded 49h expressions; everything else
-- keys off is_late_filing / is_matured since migration 224):
--   1. analytics.mv_hr_report_review: deadline_at, is_late_filing,
--      is_missing_report, is_matured (the 230-pattern v2 swap; body =
--      migration 230's / the live definition verbatim, only the four interval
--      expressions gain the dow CASE).
--   2. Serving view analytics.v_hr_report_review repointed, column list
--      identical to 230's (39 columns, no change).
--   3. analytics.hr_review_backlog(): overdue-days anchor gains the same CASE
--      (body otherwise = migration 179's).
--   4. agent.schema_metadata descriptions.
-- App-side twins (ontel-people feat/friday-filing-60h):
--   lib/hr/domain/filing-deadline.ts (49h/61h + 48h/60h constants),
--   missing-days.ts, report-reminders.ts, migration 032.
--
-- Rollback at the bottom. NEVER DROP analytics.v_hr_report_review.

SET LOCAL lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0) Preflight: 230 must be applied (has_undertime present), 232 must not be.
-- ---------------------------------------------------------------------------
DO $$
DECLARE cur text;
BEGIN
  cur := pg_get_viewdef('analytics.mv_hr_report_review'::regclass);
  IF position('has_undertime' IN cur) = 0 THEN
    RAISE EXCEPTION '232: mv lacks has_undertime; 230 missing or drifted';
  END IF;
  IF position('61:00:00' IN cur) > 0 THEN
    RAISE EXCEPTION '232: mv already Friday-aware; already applied';
  END IF;
  IF to_regclass('analytics.mv_hr_report_review_v2') IS NOT NULL THEN
    RAISE EXCEPTION '232: mv_hr_report_review_v2 already exists; clean up before re-running';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1) New review MV: body = the live definition (migration 230's), verbatim,
--    with the four deadline expressions Friday-aware.
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
    r.first_clock_in + CASE WHEN EXTRACT(dow FROM b.work_date) = 5 THEN '61:00:00'::interval ELSE '49:00:00'::interval END AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL AND t.submitted_on >= (r.first_clock_in + CASE WHEN EXTRACT(dow FROM b.work_date) = 5 THEN '61:00:00'::interval ELSE '49:00:00'::interval END) AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + CASE WHEN EXTRACT(dow FROM b.work_date) = 5 THEN '61:00:00'::interval ELSE '49:00:00'::interval END) AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + CASE WHEN EXTRACT(dow FROM b.work_date) = 5 THEN '61:00:00'::interval ELSE '49:00:00'::interval END) AS is_matured,
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
-- 2) Repoint the serving view (column list identical to 230's, no change),
--    drop the old MV, rename v2 into place.
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
  'HR daily-report review serving view over mv_hr_report_review. Filing deadline per migration 232: clock-in + 49h, or + 61h when work_date is a Friday (weekend extension); drives deadline_at, is_late_filing, is_missing_report, is_matured. is_tardy per 215/230 (grace 30m, excludes cancelled, Sat/Sun and undertime DRs). is_late_filing excludes cancelled DRs. first_task_start/long_gap_* per 222/223; early/late_task_count per 217.';

DROP MATERIALIZED VIEW analytics.mv_hr_report_review;

ALTER MATERIALIZED VIEW analytics.mv_hr_report_review_v2 RENAME TO mv_hr_report_review;
ALTER INDEX analytics.mv_hr_report_review_v2_pk RENAME TO mv_hr_report_review_pk;
ALTER INDEX analytics.idx_mv_hr_review_v2_work_date RENAME TO idx_mv_hr_review_work_date;
ALTER INDEX analytics.idx_mv_hr_review_v2_carrier_date RENAME TO idx_mv_hr_review_carrier_date;

-- ---------------------------------------------------------------------------
-- 3) hr_review_backlog: overdue-days anchor follows the same Friday-aware
--    deadline (body otherwise = migration 179's).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION analytics.hr_review_backlog()
 RETURNS jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO 'analytics', 'data_staging', 'reference'
AS $function$
  with m as (
    select extract(epoch from (now() - (evidence_at
             + case when extract(dow from work_date) = 5
                    then interval '61 hours' else interval '49 hours' end))) / 86400.0 as overdue_days
    from analytics.v_hr_report_review
    where is_missing_report
  ), b as (
    select
      count(*)                                                        as total,
      round(max(overdue_days)::numeric, 1)                            as oldest_days,
      count(*) filter (where overdue_days < 2)                        as d_le2,
      count(*) filter (where overdue_days >= 2  and overdue_days < 7) as d_2_7,
      count(*) filter (where overdue_days >= 7  and overdue_days < 14) as d_7_14,
      count(*) filter (where overdue_days >= 14 and overdue_days < 30) as d_14_30,
      count(*) filter (where overdue_days >= 30)                      as d_30p
    from m
  )
  select jsonb_build_object(
    'total', (select total from b),
    'oldest_days', (select oldest_days from b),
    'buckets', jsonb_build_array(
      jsonb_build_object('label', '≤2d',    'n', (select d_le2 from b)),
      jsonb_build_object('label', '2–7d',   'n', (select d_2_7 from b)),
      jsonb_build_object('label', '7–14d',  'n', (select d_7_14 from b)),
      jsonb_build_object('label', '14–30d', 'n', (select d_14_30 from b)),
      jsonb_build_object('label', '30d+',   'n', (select d_30p from b))
    )
  );
$function$;

-- ---------------------------------------------------------------------------
-- 4) Semantic-layer metadata: note the Friday exception.
-- ---------------------------------------------------------------------------
UPDATE agent.schema_metadata
SET description = description ||
  ' Friday work dates (extract(dow)=5) get a weekend extension: filing deadline 61h after clock-in instead of 49h (migration 232).'
WHERE schema_name = 'analytics'
  AND table_name IN ('v_hr_report_review', 'mv_hr_report_review', 'hr_review_summary', 'hr_review_backlog', 'hr_infraction_months', 'hr_infraction_detail')
  AND description NOT LIKE '%migration 232%';

-- ---------------------------------------------------------------------------
-- 5) PostgREST schema reload (definition change only, same columns).
-- ---------------------------------------------------------------------------
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- ROLLBACK (behavioral; NEVER DROP v_hr_report_review):
--   Re-run THIS file with the four CASE expressions in step 1 and the CASE in
--   step 3 reverted to plain '49:00:00'::interval / interval '49 hours'
--   (i.e. migration 230's step 1 + 179's backlog body), and drop the metadata
--   sentence added in step 4.
-- ---------------------------------------------------------------------------
