-- 215: Suppress late-clock-in and late-filing signals for cancelled DRs and for
-- weekend work. Jamil 2026-08-03.
-- Spec: ontel-people/docs/superpowers/specs/2026-08-03-cancelled-and-weekend-signal-suppression-design.md
--
-- Three rules. Two of them are defined here, once each:
--   is_tardy (NEW)   clock_in_late_minutes > 30 AND not cancelled AND not Sat/Sun
--   is_late_filing   gains AND not cancelled
-- The third (reminder eligibility) lives in ontel-people migration 029
-- (originally 028; renamed 2026-08-03, see below).
--
-- Weekend affects is_tardy ONLY. A Saturday report is still owed, so late FILING
-- on weekend work stays a live signal. Only the clock-in-vs-roster-shift
-- comparison is suppressed: ref_employees.shift_time_in_pht describes the weekday
-- roster, and members legitimately start weekend work at other times. Measured
-- 2026-08-03 over 90 days of non-cancelled rows, the Saturday tardy rate is 24.3%
-- against 1.7% Mon-Fri, and 119 of 255 tardy flags system-wide are weekend rows.
--
-- v_hr_report_review is a PLAIN VIEW over mv_hr_report_review, and every consumer
-- reads the view rather than the matview (verified 2026-08-03 against pg_proc:
-- report_reminder_candidates, hr_review_page, hr_review_count,
-- hr_infraction_months, hr_infraction_detail). So this migration is
-- CREATE OR REPLACE only: no matview rebuild and no REFRESH, which deliberately
-- avoids the refresh-order hazard found during the DRMC brownout fix (2026-07-31).
-- NEVER DROP analytics.mv_hr_report_review.
--
-- Rollback is at the bottom of this file.
--
-- Renumbered 214 -> 215 on 2026-08-03: another session applied
-- 214_mv_timer_revenue_daily.sql to the warehouse four minutes after this one, so
-- this file was renamed to the next free number to resolve the file-naming
-- collision. Already applied to the database under the original name; the
-- Supabase migration registry still holds the row as "214" for history. Nothing
-- was re-applied.

-- ---------------------------------------------------------------------------
-- 1. v_hr_report_review: redefine is_late_filing in place, append is_tardy last.
--    Existing column names, types, and order are preserved, so CREATE OR REPLACE
--    is legal and nothing that reads the view can break.
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
  -- A cancelled DR is never a late filing. Today this is defensive: Swift has
  -- never produced a submitted-then-cancelled DR (0 rows all-time as of
  -- 2026-08-03), and is_late_filing requires submitted_on IS NOT NULL. The clause
  -- removes the dependency on that undocumented Swift behaviour.
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
  -- THE tardy verdict. Every consumer reads this column; nothing re-derives it.
  -- COALESCE is load-bearing: clock_in_late_minutes is NULL on ~47% of rows (no
  -- clock-in, or no parseable roster shift), so the bare expression would yield
  -- NULL rather than false. 30 = LATE_GRACE_MINUTES (lib/hr/domain/late-clock-in.ts);
  -- migration 163 floors seconds, so > 30 means elapsed >= 31:00 exactly.
  -- work_dow: 0 = Sunday, 6 = Saturday.
  COALESCE(
    m.clock_in_late_minutes > 30
      AND m.task_status IS DISTINCT FROM 'cancelled'
      AND m.work_dow NOT IN (0, 6)
  , false) AS is_tardy
FROM analytics.mv_hr_report_review m;

COMMENT ON VIEW analytics.v_hr_report_review IS
  'HR daily-report review serving view over mv_hr_report_review. is_tardy is the single tardy verdict (grace 30m, excludes cancelled DRs and Sat/Sun work); is_late_filing excludes cancelled DRs. Migration 215.';

-- ---------------------------------------------------------------------------
-- 2. v_daily_report_approvals: append is_tardy so the DR Approval badge follows
--    the same rule as DR Monitoring.
--
--    This view is ~5.6k chars over base tables and its clock_in_late_minutes is a
--    multi-line CASE containing a regex (verified 2026-08-03), so re-pasting or
--    string-injecting that expression is exactly the hand-escaping hazard
--    migration 200 warned about. Instead we wrap the live definition in a
--    subquery and derive is_tardy from the already-computed OUTPUT column.
--
--    Safe because (verified 2026-08-03): the view has 33 columns with 33 distinct
--    names, so b.* expands cleanly and in the original order, which is what
--    CREATE OR REPLACE requires; and task_status, work_dow and
--    clock_in_late_minutes are all present on it.
--
--    Idempotent via the column-exists guard. Without that guard a re-run would
--    nest the wrapper, so do not remove it.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  body text;
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'analytics' AND table_name = 'v_daily_report_approvals' AND column_name = 'is_tardy'
  ) THEN
    RAISE NOTICE '215: is_tardy already present on v_daily_report_approvals; skipping.';
    RETURN;
  END IF;

  -- pg_get_viewdef returns a trailing semicolon, which cannot appear inside a
  -- subquery; strip it.
  body := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass, true)), ';');

  IF position('clock_in_late_minutes' IN body) = 0
     OR position('work_dow' IN body) = 0
     OR position('task_status' IN body) = 0 THEN
    RAISE EXCEPTION '215: v_daily_report_approvals is missing an input column for is_tardy, aborting.';
  END IF;

  EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
    SELECT b.*,
           COALESCE(b.clock_in_late_minutes > 30
                    AND b.task_status IS DISTINCT FROM ''cancelled''
                    AND b.work_dow NOT IN (0, 6), false) AS is_tardy
    FROM (' || body || ') b';
END $$;

-- ---------------------------------------------------------------------------
-- 3. Repoint the four RPCs at is_tardy. Same anchored-replace idiom, applied to
--    pg_get_functiondef output (which is already a complete CREATE OR REPLACE
--    FUNCTION statement, so the result can be EXECUTEd directly).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  fn      text;
  def     text;
  anchor  text := 'or (''tardy''        = any(p_flags) and v.clock_in_late_minutes > 30)';
  repl    text := 'or (''tardy''        = any(p_flags) and v.is_tardy)';
BEGIN
  FOREACH fn IN ARRAY ARRAY['hr_review_page', 'hr_review_count'] LOOP
    SELECT pg_get_functiondef(p.oid) INTO def
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'analytics' AND p.proname = fn;

    IF def IS NULL THEN
      RAISE EXCEPTION '215: analytics.% not found, aborting.', fn;
    END IF;
    IF position(repl IN def) > 0 THEN
      RAISE NOTICE '215: analytics.% already uses is_tardy; skipping.', fn;
      CONTINUE;
    END IF;
    IF position(anchor IN def) = 0 THEN
      RAISE EXCEPTION '215: tardy anchor not found in analytics.%, aborting.', fn;
    END IF;

    EXECUTE replace(def, anchor, repl);
  END LOOP;
END $$;

DO $$
DECLARE
  def     text;
  a_file  text := '(is_late_filing and task_status <> ''cancelled'')             as filing_inf,';
  r_file  text := 'is_late_filing                                               as filing_inf,';
  a_tardy text := '(clock_in_late_minutes > 30 and task_status <> ''cancelled'') as tardy_inf';
  r_tardy text := 'is_tardy                                                     as tardy_inf';
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO def
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'analytics' AND p.proname = 'hr_infraction_months';

  IF def IS NULL THEN
    RAISE EXCEPTION '215: analytics.hr_infraction_months not found, aborting.';
  END IF;
  IF position(r_tardy IN def) > 0 THEN
    RAISE NOTICE '215: hr_infraction_months already uses is_tardy; skipping.';
    RETURN;
  END IF;
  IF position(a_file IN def) = 0 OR position(a_tardy IN def) = 0 THEN
    RAISE EXCEPTION '215: infraction anchors not found in hr_infraction_months, aborting.';
  END IF;

  EXECUTE replace(replace(def, a_file, r_file), a_tardy, r_tardy);
END $$;

DO $$
DECLARE
  def    text;
  anchor text := 'when p_track = ''tardiness'' then v.clock_in_late_minutes > 30';
  repl   text := 'when p_track = ''tardiness'' then v.is_tardy';
BEGIN
  SELECT pg_get_functiondef(p.oid) INTO def
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE n.nspname = 'analytics' AND p.proname = 'hr_infraction_detail';

  IF def IS NULL THEN
    RAISE EXCEPTION '215: analytics.hr_infraction_detail not found, aborting.';
  END IF;
  IF position(repl IN def) > 0 THEN
    RAISE NOTICE '215: hr_infraction_detail already uses is_tardy; skipping.';
    RETURN;
  END IF;
  IF position(anchor IN def) = 0 THEN
    RAISE EXCEPTION '215: tardiness anchor not found in hr_infraction_detail, aborting.';
  END IF;

  EXECUTE replace(def, anchor, repl);
END $$;

-- hr_infraction_detail keeps its own `and v.task_status <> 'cancelled'` filter.
-- It is now redundant (both is_tardy and is_late_filing already exclude cancelled)
-- but harmless, and leaving it keeps this migration to one edit per function.

-- ---------------------------------------------------------------------------
-- 4. PostgREST must re-read the schema before the app can select is_tardy.
-- ---------------------------------------------------------------------------
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- ROLLBACK
--   1. Re-run migration 207's v_hr_report_review definition (restores
--      is_late_filing and drops is_tardy). CREATE OR REPLACE cannot drop a
--      column, so this needs DROP VIEW analytics.v_hr_report_review followed by
--      the 207 body. Do NOT drop mv_hr_report_review.
--   2. Re-run migration 200 for v_daily_report_approvals (same DROP + recreate).
--   3. Re-run migrations 199 and 177 verbatim to restore the four RPCs.
--   4. NOTIFY pgrst, 'reload schema';
-- ---------------------------------------------------------------------------
