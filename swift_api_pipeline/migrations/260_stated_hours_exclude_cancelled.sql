-- 260: a cancelled DR requirement no longer counts toward stated hours.
-- (Jamil 2026-09-09: Mark Manalac 260501, work date 2026-05-14 showed "18 hrs"
-- in the DRMC day panel. The report has two requirements, 9h submitted and 9h
-- cancelled; the header, the timeline "stated hours" and HR review all summed
-- both. It should read 9.)
--
-- Root cause: analytics.mv_daily_report_task_rollup (migration 140) computes
--   sum(hours_worked) ... GROUP BY task_did
-- over data_staging.stg_daily_report_hours with no req_status filter, and
-- v_daily_report_approvals exposes that sum as total_hours. Every stated-hours
-- surface reads it: DR Approval queue/browse/drawer (PostgREST on the view),
-- the timeline panel, mv_hr_report_review.stated_hours / stated_hours_net /
-- variance_hours / coverage_pct (b.total_hours from the view), the member
-- weekly report, the Ops report and analytics.dr_attachment_counts (hours
-- range filters). Measured 2026-09-09: 26 cancelled rows / 126.5 h across
-- 19 task_dids; 11 rows of v_daily_report_approvals change (-81.5 h; the
-- other 45 h sit on tasks the view excludes: work_date NULL or in the future).
--
-- Why not fix the MV: an MV cannot be redefined in place, and
-- mv_daily_report_task_rollup is a catalog dependency of BOTH
-- v_daily_report_approvals and mv_hr_report_review (the NEVER-DROP brownout
-- chain, migration 207). mv_hr_report_review takes only r.first_clock_in from
-- the rollup and its stated hours from b.total_hours, so patching the VIEW is
-- sufficient and reaches every consumer: PostgREST readers immediately, the
-- review MV on the next 5-min refresh_dr_task_rollup_safe() tick.
--
-- Fix: replace `r.total_hours` in v_daily_report_approvals with a per-row
-- filtered sum straight off stg_daily_report_hours:
--   ( SELECT sum(h.hours_worked) FROM data_staging.stg_daily_report_hours h
--     WHERE h.task_did = t.task_did AND h.req_status IS DISTINCT FROM 'cancelled')
-- Probed via the (task_did, req_id) unique index, same pattern as the 230
-- has_undertime EXISTS. Semantics: a report whose ONLY requirements are
-- cancelled now has total_hours NULL (no stated hours), not 9; a task with no
-- requirement rows stays NULL as before. req_count is untouched (still counts
-- every requirement, cancelled included - the drawer lists them all).
--
-- Measured on prod 2026-09-09 (temp view, then dropped):
--   rows 30,744 = 30,744; sum(total_hours) 166,751.3 -> 166,669.8 (-81.5 h);
--   full-view SELECT 407 ms / 129k buffers -> 653 ms / 241k buffers
--   (+30,744 index probes); column type numeric unchanged.
--
-- Mechanics: anchored replace on pg_get_viewdef (the 215 §3 / 230 §3 idiom) -
-- the view is ~6k chars over base tables; re-pasting by hand is the escaping
-- hazard migration 200 warned about. CREATE OR REPLACE keeps the column list,
-- so v_approver_options, v_daily_report_approver_stats, mv_hr_report_review
-- and dr_attachment_counts are untouched. Rollback at the bottom.

SET LOCAL lock_timeout = '15s';

DO $$
DECLARE
  def    text;
  anchor text := 'r.total_hours,';
  repl   text := '( SELECT sum(h.hours_worked) FROM data_staging.stg_daily_report_hours h WHERE h.task_did = t.task_did AND h.req_status IS DISTINCT FROM ''cancelled''::text) AS total_hours,';
  hits   int;
BEGIN
  def := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass)), ';');

  IF position('req_status IS DISTINCT FROM ''cancelled''' IN def) > 0 THEN
    RAISE NOTICE '260: v_daily_report_approvals already excludes cancelled requirements; skipping.';
    RETURN;
  END IF;

  hits := (length(def) - length(replace(def, anchor, ''))) / length(anchor);
  IF hits <> 1 THEN
    RAISE EXCEPTION '260: expected exactly one "%" anchor in v_daily_report_approvals, found %; aborting.', anchor, hits;
  END IF;

  EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || replace(def, anchor, repl);
END $$;

COMMENT ON VIEW analytics.v_daily_report_approvals IS
  'Daily-report approval serving view over stg_daily_reports + mv_daily_report_task_rollup. total_hours = sum of hours_worked over the report''s requirements EXCLUDING req_status = cancelled (migration 260); NULL when no live requirement. req_count still counts every requirement. is_tardy per 215/230.';

-- PostgREST caches the view definition; force a re-read.
NOTIFY pgrst, 'reload schema';

-- Verify:
--   SELECT employee_name, work_date, total_hours FROM analytics.v_daily_report_approvals
--   WHERE task_did = '-OrpVC3sqR_pzcVFh59q-OhBjAZLtnFfaXVrRlrX';        -- 9, was 18
--   SELECT count(*) FROM analytics.v_daily_report_approvals;              -- 30,744 (2026-09-09)
--   SELECT stated_hours FROM analytics.v_hr_report_review
--   WHERE task_did = '-OrpVC3sqR_pzcVFh59q-OhBjAZLtnFfaXVrRlrX';          -- 9 after the next
--   -- 5-min refresh, or immediately after SELECT analytics.refresh_dr_task_rollup_safe();
--
-- Rollback (behavioral; NEVER DROP the view). Postgres re-serialises the
-- subquery with an "AS sum" alias, extra parentheses and line breaks, so the
-- reverse replace is a whitespace-tolerant regexp (verified against the live
-- text 2026-09-09):
--   DO $$
--   DECLARE def text; new_def text;
--   BEGIN
--     def := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass)), ';');
--     new_def := regexp_replace(def,
--       '\( SELECT sum\(h\.hours_worked\) AS sum\s+FROM data_staging\.stg_daily_report_hours h\s+WHERE \(\(h\.task_did = t\.task_did\) AND \(h\.req_status IS DISTINCT FROM ''cancelled''::text\)\)\) AS total_hours,',
--       'r.total_hours,');
--     IF new_def = def THEN RAISE EXCEPTION '260 rollback: subquery text not found'; END IF;
--     EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || new_def;
--   END $$;
--   NOTIFY pgrst, 'reload schema';
--   SELECT analytics.refresh_dr_task_rollup_safe();   -- re-snapshot HR review
