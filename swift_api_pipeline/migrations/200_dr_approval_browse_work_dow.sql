-- 200: Add work_dow to analytics.v_daily_report_approvals (DR Approval browse). Jamil 2026-07-29.
-- The DR Approval "Work date" column header gains a day-of-week picker, matching
-- DR Monitoring. That filter needs a precomputed weekday column to push down to
-- PostgREST (.in("work_dow", dows)); the browse view had work_date but no work_dow.
-- Mirrors the review view (v_hr_report_review.work_dow): EXTRACT(dow) 0=Sun..6=Sat.
--
-- work_dow is APPENDED as the last SELECT column, so CREATE OR REPLACE VIEW is a
-- safe additive change (existing column order/names/types are untouched; nothing
-- that reads the view can break). Rather than re-paste the ~60-line view body
-- (whose clock_in_late_minutes regex escaping is easy to get wrong by hand), we
-- read the live definition and inject the column right before the main FROM,
-- anchored on a stable substring. Idempotent: no-op if work_dow already exists;
-- aborts loudly if the anchor is gone (the view shape changed upstream), so this
-- never silently produces a wrong definition.
--
-- Rollback: CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS <same body
-- without the work_dow line> (or restore from the migration that last defined it).

DO $$
DECLARE
  body   text;
  anchor text := 'th.person_key IS NOT NULL AS has_timer_history' || E'\n   FROM data_staging.stg_daily_reports t';
  repl   text := 'th.person_key IS NOT NULL AS has_timer_history,' || E'\n    EXTRACT(dow FROM t.work_date)::smallint AS work_dow' || E'\n   FROM data_staging.stg_daily_reports t';
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'analytics' AND table_name = 'v_daily_report_approvals' AND column_name = 'work_dow'
  ) THEN
    RAISE NOTICE '200: work_dow already present on v_daily_report_approvals; skipping.';
    RETURN;
  END IF;

  body := pg_get_viewdef('analytics.v_daily_report_approvals'::regclass, true);
  IF position(anchor IN body) = 0 THEN
    RAISE EXCEPTION '200: anchor not found in v_daily_report_approvals; view shape changed, aborting.';
  END IF;

  EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || replace(body, anchor, repl);
END $$;
