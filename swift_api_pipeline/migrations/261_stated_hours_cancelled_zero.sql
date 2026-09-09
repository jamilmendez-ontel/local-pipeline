-- 261: stated hours of a cancelled daily report are 0, not blank.
-- (Jamil 2026-09-09, on the 260 result: "the stated hours in a cancelled task
-- should automatically become 0".)
--
-- 260 made v_daily_report_approvals.total_hours the sum of the report's
-- non-cancelled requirements and left it NULL when nothing live remained.
-- Rule from here:
--   task_status = 'cancelled'                        -> 0   (whatever the requirements say)
--   requirement rows exist, every one cancelled      -> 0
--   requirement rows exist, some live                -> sum of the live ones
--   no requirement rows at all (not filed yet)       -> NULL (unchanged)
--
-- Measured on prod 2026-09-09 (temp view analytics.v_daily_report_approvals_test_261,
-- dropped): 7,573 rows change, all to 0 -- 7,568 cancelled tasks (7,564 were NULL
-- with no requirements, 4 had live requirements totalling 33 h) plus 5 filed
-- reports (3 approved, 2 submitted) whose only requirement is cancelled. Rows
-- 30,744 unchanged; full-view SELECT 611 ms (the CASE skips the requirement
-- probe for cancelled tasks, so slightly cheaper than 260's 653 ms).
--
-- Downstream, checked before applying:
--   * Hours Analysis / variance report / Ops report read only rows with
--     coverage_pct IS NOT NULL, which mv_hr_report_review computes only when
--     stated_hours_net > 0; a cancelled task at 0 h has net 0, so it stays out.
--   * hr_review_summary / hr_review_count / infraction RPCs already exclude
--     task_status = 'cancelled' from matured / late / tardy.
--   * member weekly packs exclude cancelled days in member-week-model.
--   * 43 cancelled tasks carry timer minutes (80.8 h); their HR review row now
--     shows stated 0 / variance_hours = -timed instead of blanks. Honest, and
--     no aggregate reads it.
--   * DR Approval "stated hours" range filters with a 0 lower bound now match
--     cancelled tasks (total_hours is no longer NULL for them) -- the intended
--     meaning of the rule.
--
-- Mechanics: whitespace-tolerant regexp replace of the exact subquery 260
-- installed (Postgres re-serialises it with an "AS sum" alias and line breaks),
-- CREATE OR REPLACE keeps the column list and numeric type. Rollback at the bottom.

SET LOCAL lock_timeout = '15s';

DO $$
DECLARE
  def     text;
  new_def text;
  pattern text := '\( SELECT sum\(h\.hours_worked\) AS sum\s+FROM data_staging\.stg_daily_report_hours h\s+WHERE \(\(h\.task_did = t\.task_did\) AND \(h\.req_status IS DISTINCT FROM ''cancelled''::text\)\)\) AS total_hours,';
  repl    text := 'CASE WHEN t.task_status = ''cancelled''::text THEN 0::numeric ELSE ( SELECT CASE WHEN count(*) = 0 THEN NULL::numeric ELSE COALESCE(sum(h.hours_worked) FILTER (WHERE h.req_status IS DISTINCT FROM ''cancelled''::text), 0::numeric) END FROM data_staging.stg_daily_report_hours h WHERE h.task_did = t.task_did) END AS total_hours,';
BEGIN
  def := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass)), ';');

  -- pg_get_viewdef re-serialises the applied expression as "THEN (0)::numeric"
  -- and "FILTER (WHERE (h.req_status ..." (parenthesised constants/predicates).
  IF position('THEN (0)::numeric' IN def) > 0 AND position('FILTER (WHERE (h.req_status' IN def) > 0 THEN
    RAISE NOTICE '261: v_daily_report_approvals already zeroes cancelled reports; skipping.';
    RETURN;
  END IF;

  new_def := regexp_replace(def, pattern, repl);
  IF new_def = def THEN
    RAISE EXCEPTION '261: the 260 total_hours subquery was not found in v_daily_report_approvals; apply 260 first or check pg_get_viewdef.';
  END IF;

  EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || new_def;
END $$;

COMMENT ON VIEW analytics.v_daily_report_approvals IS
  'Daily-report approval serving view over stg_daily_reports + mv_daily_report_task_rollup. total_hours (migrations 260/261): 0 for a cancelled task or when every requirement is cancelled; otherwise the sum of the non-cancelled requirements; NULL only when the report has no requirement rows yet. req_count still counts every requirement. is_tardy per 215/230.';

NOTIFY pgrst, 'reload schema';

-- Verify:
--   SELECT total_hours FROM analytics.v_daily_report_approvals
--   WHERE task_did = '-OrpVC3sqR_pzcVFh59q-OhBjAZLtnFfaXVrRlrX';        -- still 9
--   SELECT count(*) FROM analytics.v_daily_report_approvals
--   WHERE task_status = 'cancelled' AND total_hours IS DISTINCT FROM 0;  -- 0
--   SELECT count(*) FROM analytics.v_daily_report_approvals v
--   WHERE total_hours IS NULL AND req_count > 0;                         -- 0
--   SELECT analytics.refresh_dr_task_rollup_safe();                      -- HR review now
--
-- Rollback (behavioral; NEVER DROP the view): restore the 260 expression.
--   DO $$
--   DECLARE def text; new_def text;
--   BEGIN
--     def := rtrim(btrim(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass)), ';');
--     new_def := regexp_replace(def,
--       'CASE\s+WHEN \(t\.task_status = ''cancelled''::text\) THEN \(0\)::numeric\s+ELSE \( SELECT\s+CASE\s+WHEN \(count\(\*\) = 0\) THEN NULL::numeric\s+ELSE COALESCE\(sum\(h\.hours_worked\) FILTER \(WHERE \(h\.req_status IS DISTINCT FROM ''cancelled''::text\)\), \(0\)::numeric\)\s+END AS "coalesce"\s+FROM data_staging\.stg_daily_report_hours h\s+WHERE \(h\.task_did = t\.task_did\)\)\s+END AS total_hours,',
--       '( SELECT sum(h.hours_worked) FROM data_staging.stg_daily_report_hours h WHERE h.task_did = t.task_did AND h.req_status IS DISTINCT FROM ''cancelled''::text) AS total_hours,');
--     IF new_def = def THEN RAISE EXCEPTION '261 rollback: expression not found, check pg_get_viewdef'; END IF;
--     EXECUTE 'CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS ' || new_def;
--   END $$;
--   NOTIFY pgrst, 'reload schema';
--   SELECT analytics.refresh_dr_task_rollup_safe();
