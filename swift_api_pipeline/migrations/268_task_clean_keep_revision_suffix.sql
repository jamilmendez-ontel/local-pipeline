-- =============================================================================
-- 268_task_clean_keep_revision_suffix.sql
--
-- APPLIED 2026-09-23 02:45-03:05 ET (code merged first: local-pipeline 5bb3798).
--   Backfill, rows updated / expected:
--     stg_asset_tasks            479,291 / 479,291   (253.6s)
--     stg_asset_tasks_inc        215,728 / 215,728   ( 55.7s)
--     stg_qa_form                147,994 / 147,994   ( 24.5s)
--     stg_timer_activities       129,920 / 129,920   ( 30.8s)
--     stg_timer_activities_clean 129,298 / 129,298   ( 32.4s)
--     stg_gc_tracker_tasks         6,214 /   6,214   ( 11.6s)
--     stg_user_priorities            581 /     581   (  0.3s)
--     app_timer.entry_additions          5 /       5
--     stg_asset_tasks_gc                 0 /       0   (empty)
--   Every count matched the preflight estimate exactly. Assertion: all 9 pass.
--
--   VERIFIED after apply:
--     stg_timer_activities_clean NULL task_clean   119,761 -> 0
--     stg_timer_activities_clean prefixed            2,782 -> 0
--     rate keys reachable                          36 -> 54 of 60 (as predicted)
--     approved-LR asset set                    18,484 -> 18,483 (the one predicted asset)
--     agent.schema_metadata rows stating the old rule    4 -> 0
--     mv_timer_revenue  133,943 -> 208,275 rows; priced 192,456 rows / $12,721,122.96
--       The +74,332 is dominated by 2025 (71,145 rows, $4,320,793.13), which could
--       never be priced while its task_clean was NULL. That revenue was always
--       earned; it was simply unattributable. The rule change itself re-rates
--       7,725 rows across 72 revision labels, $157,168.99.
--     mv_po_issued_status 661 -> 767 and mv_qp_to_po_duration 220 -> 266 are NOT
--       effects of this change ('PO Issued'/'Quote Provided' identities are
--       unchanged). Neither MV is in refresh_analytics() or on cron, so nothing
--       had refreshed them; the jump is accumulated staleness being corrected.
--
--   GOTCHA worth remembering: the first apply aborted at the assertion because
--   the 5-minute user_priorities_extract was still running pre-merge code from
--   GitHub main and rewrote 581 rows with the old rule mid-migration. Push main
--   BEFORE applying, not just merge locally.
--
-- RULE CHANGE (Jamil, 2026-09-23). The cleaned task-name columns keep the
-- trailing revision number; only the leading sequence prefix is stripped.
--
--   OLD:  TRIM(regexp_replace(regexp_replace(t,'^([0-9]+[a-zA-Z]?\. *)+',''),'\s+[0-9]+$',''))
--   NEW:  BTRIM(regexp_replace(t,'^([0-9]+[a-zA-Z]?\.[[:space:]]*)+',''), E' \t\n\r\f\v')
--
--   "7. COP Revision Complete 2"  ->  "COP Revision Complete 2"   (was "COP Revision Complete")
--   "3F. COP Upload Complete"     ->  "COP Upload Complete"       (unchanged intent)
--
-- The whitespace class is '[[:space:]]' + BTRIM over the explicit ASCII set so the
-- SQL agrees byte-for-byte with the Python mirrors, which use [ \t\n\r\f\v] and
-- .strip(' \t\n\r\f\v') rather than \s / bare .strip() (Python's \s and .strip()
-- also match NBSP, which Postgres does not). Verified a no-op on current data:
-- 0 of 4,486,170 rows differ between the simple and the hardened expression.
--
-- WHY. task_clean is the revenue join key
-- (analytics.mv_timer_revenue: ref_task_revenue_rates.task_name_norm = task_clean).
-- The rate sheet prices revisions below first passes (AT&T Live Review Complete
-- $200 vs "Live Review Complete 2" $83; COP Revision Complete $30 / "2" $20 /
-- "3" $10). Because the suffix was stripped, most of the 60 rate keys were
-- unreachable: 36 reachable today, 54 after this change.
-- This migration restates history: ~6,160 h across 10,738 timer rows move onto
-- their own revision rate.
--
-- ALSO FIXED HERE (defects found while auditing the column):
--   (a) All of calendar 2025 had task_clean = NULL (119,761 rows in
--       stg_timer_activities_clean) -- the historical backfill loaded `task`
--       without ever computing the clean name.
--   (b) 2,782 rows kept lettered prefixes ("3F. COP Upload Complete") because
--       migration 013 used '^\d+\.\s*' with no [a-zA-Z]? and no repeat.
--   (c) rebuild_timer_clean()'s corrections branch re-derived task_clean with a
--       weaker regex ('^\d+\.\s+') than every other branch. Now canonical.
--
-- KNOWN SIDE EFFECT (measured, accepted). Seven analytics objects filter
-- task_name_clean against hardcoded literals. Measured on stg_asset_tasks:
-- 'PO Issued', 'Quote Provided', '48Hr / Test Package Complete' and
-- 'Final COP Complete' have ZERO rows that change identity, so
-- mv_po_issued_status, mv_qp_to_po_duration, v_page5_po_status, v_page5_qp_to_po,
-- v_cop_date_check and v_48hr_date_check (the 6 PM PHT Date Validator chain) are
-- unaffected. 'Live Review Complete' does split: 36,869 rows become
-- 'Live Review Complete 2' (1,143 of them approved). Net effect on
-- mv_timer_revenue's approved-LR asset set is 18,484 -> 18,483, i.e. exactly ONE
-- asset loses approved-LR coverage. Spot-check that asset's revenue row after the
-- refresh.
--
-- NOT TOUCHED: gap_report.* and scorecard.* read these staging tables and carry
-- their own task_name_clean copies (mv_timer_base, mv_timer_classified,
-- mv_user_priorities, mv_asset_tasks_recent, mv_nonvzw_*). They are other teams'
-- schemas; their MVs shift when they next refresh. Same for autountrack.v_untrack_queue.
-- Flagged to Jamil, not changed here.
--
-- Code side: local-pipeline transform.py (clean_task_name + the 4 SQL exprs) and
-- timer_correction_review.py, gc-asset-lake transform.py,
-- ontel-data-platform asset_fields.py.
--
-- -----------------------------------------------------------------------------
-- HOW TO APPLY -- READ THIS FIRST
--
--   Section 2 calls a procedure that issues COMMIT inside its batch loop. A
--   procedure cannot COMMIT inside an implicit transaction block, so this file
--   MUST NOT be sent as one multi-statement string. That rules out the Supabase
--   SQL editor, the MCP apply_migration tool, and `psql --single-transaction`/-1.
--
--   Apply with plain psql, one statement per round trip:
--       psql "$CONN" -v ON_ERROR_STOP=1 -f 268_task_clean_keep_revision_suffix.sql
--   or with the statement-splitting applier used for this migration.
--
--   RESUMING after an interrupt: section 1 is idempotent and the guard below
--   accepts both the pre-268 and the post-268 function body, so the whole file
--   can simply be re-run. The backfill skips rows that are already correct.
--
--   WINDOW: start at 11:00 ET. From 7 days of pipeline.pipeline_runs, 11:00 and
--   17:00 ET are the only hours with zero asset-task and zero timer extracts;
--   11:00 gives a full clean hour. Avoid 00, 01 and 06 ET. Expect 22-40 min for
--   sections 2-3 and 8-20 min for the MV refreshes. Confirm >4 GB free disk on
--   the Supabase dashboard first (WAL cost is 4-7 GB against a ~23.6 GB/day
--   baseline; max_wal_size is 4 GB so several checkpoints will fire).
-- =============================================================================

BEGIN;

-- Guard: accept EITHER the 259 body (first apply) or a body that already carries
-- the 268 regex (re-apply / resume). Anything else means unexpected drift.
-- Inner search strings are dollar-quoted so the regexes need no escaping.
DO $guard$
DECLARE def text; m text;
BEGIN
  def := pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure);
  m := md5(def);
  IF m = '83b5ef9df31d0b85c634969df116c7d7' THEN
    RAISE NOTICE '268: live function is the expected 259 body; replacing';
  ELSIF position($q$BTRIM(regexp_replace(corr.task, '^([0-9]+[a-zA-Z]?\.[[:space:]]*)+', '')$q$ IN def) > 0 THEN
    RAISE NOTICE '268: live function already carries the 268 regex; section 1 is a no-op re-apply';
  ELSE
    RAISE EXCEPTION '268: live rebuild_timer_clean is neither the 259 body nor a 268 body (md5 %); re-capture before applying', m;
  END IF;
END $guard$;

-- -----------------------------------------------------------------------------
-- 1. rebuild_timer_clean(): 259's body verbatim, corrections branch regex fixed.
--    Verified byte-exact against the live definition: that single line is the
--    only difference.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION data_staging.rebuild_timer_clean()
 RETURNS void
 LANGUAGE plpgsql
 SET statement_timeout TO '300s'
AS $function$
DECLARE
    v_deleted  bigint;
    v_inserted bigint;
BEGIN
    -- 259: serialize overlapping calls. The pre-257 body opened with an
    -- unconditional DELETE of every clean row, which row-locked the table and
    -- made a second concurrent call wait for the first to commit; the
    -- change-only sync has no such barrier, so two calls racing on a
    -- brand-new correction/addition could both insert it (two ids, one
    -- natural key). A transaction-scoped advisory lock restores the queueing
    -- without touching a single row.
    PERFORM pg_advisory_xact_lock(hashtext('data_staging.rebuild_timer_clean'));

    -- 257 (WAL diet): build the WHOLE target set in a session temp table
    -- (unlogged: no WAL), then write only the difference into clean. Before
    -- this the function deleted and re-inserted all ~400k rows (571 MB, 15
    -- indexes) on every call, ~0.66 GB of WAL x ~10 calls/day (8 of them
    -- correction/removal dispatches that change a handful of rows).
    -- Every filter/step below is the 241 body verbatim with the write target
    -- renamed; the sync at the end is the only new logic.
    CREATE TEMP TABLE IF NOT EXISTS tmp_timer_clean
        (LIKE data_staging.stg_timer_activities_clean INCLUDING DEFAULTS) ON COMMIT DROP;
    TRUNCATE tmp_timer_clean;

    INSERT INTO tmp_timer_clean
    SELECT DISTINCT ON (
        t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
        t.task, t.end_time, t.duration_min
    ) t.*
    FROM data_staging.stg_timer_activities t
    WHERE
        NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.rejected_entries) rej
            WHERE r.status IN ('resolved', 'auto_resolved')
              AND r.rejected_entries IS NOT NULL
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = COALESCE((rej->>'start_time')::timestamptz, r.start_time)
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (rej->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (rej->>'duration_min')::numeric
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.duplicate_reviews r,
                 jsonb_array_elements(r.entries) e
            WHERE r.status IN ('pending', 'notified')
              AND t.project_did = r.project_did
              AND t.user_email  = r.user_email
              AND t.start_time  = (e->>'start_time')::timestamptz
              AND t.site_name IS NOT DISTINCT FROM r.site_name
              AND t.site_id   IS NOT DISTINCT FROM r.site_id
              AND t.task      IS NOT DISTINCT FROM r.task
              AND t.end_time IS NOT DISTINCT FROM (e->>'end_time')::timestamptz
              AND t.duration_min IS NOT DISTINCT FROM (e->>'duration_min')::numeric
              AND (e->>'end_time')::timestamptz < (
                  SELECT MAX((e2->>'end_time')::timestamptz)
                  FROM jsonb_array_elements(r.entries) e2
              )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM app_timer.entry_removals rm
            WHERE t.project_did = rm.project_did
              AND t.user_email  = rm.user_email
              AND t.start_time  = rm.start_time
              AND t.site_name IS NOT DISTINCT FROM rm.site_name
              AND t.site_id   IS NOT DISTINCT FROM rm.site_id
              AND t.task      IS NOT DISTINCT FROM rm.task
              AND t.end_time IS NOT DISTINCT FROM rm.end_time
              AND t.duration_min IS NOT DISTINCT FROM rm.duration_min
              AND rm.reason IS DISTINCT FROM 'REVERTED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_timer.corrections c
                  WHERE c.project_did = rm.project_did
                    AND c.user_email  = rm.user_email
                    AND c.start_time  = rm.start_time
                    AND c.site_name IS NOT DISTINCT FROM rm.site_name
                    AND c.site_id   IS NOT DISTINCT FROM rm.site_id
                    AND c.task      IS NOT DISTINCT FROM rm.task
                    AND c.end_time IS NOT DISTINCT FROM rm.end_time
                    AND c.original_duration_min IS NOT DISTINCT FROM rm.duration_min
              )
        )
    ORDER BY t.project_did, t.user_email, t.start_time, t.site_name, t.site_id,
             t.task, t.end_time, t.duration_min, t.id;

    UPDATE tmp_timer_clean t
    SET duration_min = c.corrected_duration_min,
        end_time    = c.corrected_end_time
    FROM app_timer.corrections c
    WHERE c.status = 'corrected'
      AND t.project_did = c.project_did
      AND t.user_email  = c.user_email
      AND t.start_time  = c.start_time
      AND t.site_name IS NOT DISTINCT FROM c.site_name
      AND t.site_id   IS NOT DISTINCT FROM c.site_id
      AND t.task      IS NOT DISTINCT FROM c.task
      AND t.end_time IS NOT DISTINCT FROM c.end_time
      AND t.duration_min IS NOT DISTINCT FROM c.original_duration_min;

    INSERT INTO tmp_timer_clean (
        id, project, project_number, project_did, site_name, site_id,
        task, task_clean, site_lat, site_long, user_lat, user_long,
        user_accuracy_m, site_vs_user_km, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT
        a.id, a.project, a.project_number, a.project_did, a.site_name, a.site_id,
        a.task, a.task_clean, a.site_lat, a.site_long, a.user_lat, a.user_long,
        a.user_accuracy_m, a.site_vs_user_km, a.start_time, a.end_time, a.duration_min,
        a.user_name, a.user_email, a.user_role,
        a.run_id, a.run_date,
        COALESCE(a.start_date, (a.start_time AT TIME ZONE 'America/New_York')::date),
        COALESCE(a.end_date,   (a.start_time AT TIME ZONE 'America/New_York')::date),
        a.loaded_at
    FROM app_timer.entry_additions a
    WHERE NOT EXISTS (
        SELECT 1 FROM app_timer.entry_removals rm
        WHERE rm.project_did = a.project_did
          AND rm.user_email  = a.user_email
          AND rm.start_time  = a.start_time
          AND rm.site_name IS NOT DISTINCT FROM a.site_name
          AND rm.site_id   IS NOT DISTINCT FROM a.site_id
          AND rm.task      IS NOT DISTINCT FROM a.task
          AND rm.end_time IS NOT DISTINCT FROM a.end_time
          AND rm.duration_min IS NOT DISTINCT FROM a.duration_min
          AND rm.reason IS DISTINCT FROM 'REVERTED'
    );

    INSERT INTO tmp_timer_clean (
        project, project_number, project_did, site_name, site_id,
        task, task_clean, start_time, end_time, duration_min,
        user_name, user_email, user_role,
        run_id, run_date, start_date, end_date, loaded_at
    )
    SELECT DISTINCT ON (corr.project_did, corr.user_email, corr.start_time,
                        corr.site_name, corr.site_id, corr.task)
        corr.project,
        NULL::integer AS project_number,
        corr.project_did, corr.site_name, corr.site_id,
        corr.task,
        BTRIM(regexp_replace(corr.task, '^([0-9]+[a-zA-Z]?\.[[:space:]]*)+', ''), E' \t\n\r\f\v') AS task_clean,
        corr.start_time, corr.corrected_end_time, corr.corrected_duration_min,
        nm.user_name, corr.user_email, nm.user_role,
        '00000000-0000-0000-0000-000000000002'::uuid AS run_id,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS run_date,
        (corr.start_time AT TIME ZONE 'America/New_York')::date AS start_date,
        (COALESCE(corr.corrected_end_time, corr.start_time)
            AT TIME ZONE 'America/New_York')::date AS end_date,
        NOW() AS loaded_at
    FROM app_timer.corrections corr
    LEFT JOIN (
        SELECT user_email,
               (array_agg(user_name ORDER BY start_time DESC)
                  FILTER (WHERE user_name IS NOT NULL AND user_name <> ''))[1] AS user_name,
               (array_agg(user_role ORDER BY start_time DESC)
                  FILTER (WHERE user_role IS NOT NULL AND user_role <> ''))[1] AS user_role
        FROM data_staging.stg_timer_activities
        GROUP BY user_email
    ) nm ON nm.user_email = corr.user_email
    WHERE corr.status = 'corrected'
      AND NOT EXISTS (
          SELECT 1 FROM tmp_timer_clean t
          WHERE t.project_did = corr.project_did
            AND t.user_email  = corr.user_email
            AND t.start_time  = corr.start_time
            AND t.site_name IS NOT DISTINCT FROM corr.site_name
            AND t.site_id   IS NOT DISTINCT FROM corr.site_id
            AND t.task      IS NOT DISTINCT FROM corr.task
            AND t.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND t.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      )
      -- NOTE: unlike every other removals anti-join, the LIVE function has no
      -- rm.reason REVERTED exclusion here (drift vs committed 197, preserved
      -- deliberately by 218; do not "restore" it without a decision).
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = corr.project_did
            AND rm.user_email  = corr.user_email
            AND rm.start_time  = corr.start_time
            AND rm.site_name IS NOT DISTINCT FROM corr.site_name
            AND rm.site_id   IS NOT DISTINCT FROM corr.site_id
            AND rm.task      IS NOT DISTINCT FROM corr.task
            AND rm.end_time IS NOT DISTINCT FROM corr.corrected_end_time
            AND rm.duration_min IS NOT DISTINCT FROM corr.corrected_duration_min
      );

    -- Step 5 (migration 197): drop UNTRACKED same-start runaway duplicates.
    DELETE FROM tmp_timer_clean cln
    WHERE cln.duration_min > 720
      AND EXISTS (
          SELECT 1 FROM tmp_timer_clean t2
          WHERE t2.project_did = cln.project_did
            AND t2.user_email  = cln.user_email
            AND t2.start_time  = cln.start_time
            AND t2.site_name IS NOT DISTINCT FROM cln.site_name
            AND t2.site_id   IS NOT DISTINCT FROM cln.site_id
            AND t2.task      IS NOT DISTINCT FROM cln.task
            AND t2.duration_min <= 720
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.corrections c
          WHERE c.project_did = cln.project_did
            AND c.user_email  = cln.user_email
            AND c.start_time  = cln.start_time
            AND c.site_name IS NOT DISTINCT FROM cln.site_name
            AND c.site_id   IS NOT DISTINCT FROM cln.site_id
            AND c.task      IS NOT DISTINCT FROM cln.task
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.entry_removals rm
          WHERE rm.project_did = cln.project_did
            AND rm.user_email  = cln.user_email
            AND rm.start_time  = cln.start_time
            AND rm.site_name IS NOT DISTINCT FROM cln.site_name
            AND rm.site_id   IS NOT DISTINCT FROM cln.site_id
            AND rm.task      IS NOT DISTINCT FROM cln.task
            -- 241: a removal captured while the timer was still running
            -- (end_time NULL) is not a member decision about the completed
            -- rows, so it must not shield later runaway duplicates.
            AND rm.end_time IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1 FROM app_timer.duplicate_reviews r
          WHERE r.project_did = cln.project_did
            AND r.user_email  = cln.user_email
            AND r.start_time  = cln.start_time
            AND r.site_name IS NOT DISTINCT FROM cln.site_name
            AND r.site_id   IS NOT DISTINCT FROM cln.site_id
            AND r.task      IS NOT DISTINCT FROM cln.task
      );

    -- ---------------------------------------------------------------------
    -- 257: sync the target set into clean, touching only rows that differ.
    -- ---------------------------------------------------------------------
    -- Correction-created rows (synthetic run_id ...0002) get a fresh serial
    -- id every build; reuse the clean row's id when the natural key matches
    -- so they are not deleted and re-inserted on every rebuild.
    UPDATE tmp_timer_clean t
    SET id = c.id
    FROM data_staging.stg_timer_activities_clean c
    WHERE t.run_id = '00000000-0000-0000-0000-000000000002'::uuid
      AND c.run_id = '00000000-0000-0000-0000-000000000002'::uuid
      AND c.project_did IS NOT DISTINCT FROM t.project_did AND c.user_email IS NOT DISTINCT FROM t.user_email AND c.start_time IS NOT DISTINCT FROM t.start_time AND c.site_name IS NOT DISTINCT FROM t.site_name AND c.site_id IS NOT DISTINCT FROM t.site_id AND c.task IS NOT DISTINCT FROM t.task AND c.end_time IS NOT DISTINCT FROM t.end_time AND c.duration_min IS NOT DISTINCT FROM t.duration_min;

    -- Rows that left the target set, or whose content changed (compared on
    -- every column except id and loaded_at): delete, then re-insert below.
    DELETE FROM data_staging.stg_timer_activities_clean c
    WHERE NOT EXISTS (
        SELECT 1 FROM tmp_timer_clean t
        WHERE t.id = c.id
          AND (t.project, t.project_number, t.project_did, t.site_name, t.site_id, t.task, t.site_lat, t.site_long, t.user_lat, t.user_long, t.user_accuracy_m, t.site_vs_user_km, t.start_time, t.end_time, t.duration_min, t.user_name, t.user_email, t.user_role, t.run_id, t.run_date, t.start_date, t.end_date, t.task_clean, t.asset_did) IS NOT DISTINCT FROM (c.project, c.project_number, c.project_did, c.site_name, c.site_id, c.task, c.site_lat, c.site_long, c.user_lat, c.user_long, c.user_accuracy_m, c.site_vs_user_km, c.start_time, c.end_time, c.duration_min, c.user_name, c.user_email, c.user_role, c.run_id, c.run_date, c.start_date, c.end_date, c.task_clean, c.asset_did)
    );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    INSERT INTO data_staging.stg_timer_activities_clean
    SELECT t.* FROM tmp_timer_clean t
    WHERE NOT EXISTS (SELECT 1 FROM data_staging.stg_timer_activities_clean c WHERE c.id = t.id);
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    RAISE NOTICE 'rebuild_timer_clean: % rows in target, % deleted, % inserted',
        (SELECT count(*) FROM tmp_timer_clean), v_deleted, v_inserted;
END;
$function$;

-- ROLLBACK: re-apply migration 257's CREATE OR REPLACE FUNCTION body.

COMMIT;

-- -----------------------------------------------------------------------------
-- 2. Backfill every cleaned-name column in our schemas.
--
--    ~1.11 M rows change across 8 tables. That is deliberately NOT one
--    transaction: this instance is Small tier with a standing Disk IO budget
--    concern and an active WAL diet, and a single 1.11 M-row UPDATE would hold
--    every row version until commit. The helper commits every 50,000 rows, so
--    bloat is reclaimable as it goes and an interrupted run resumes by simply
--    re-running the file (corrected rows are skipped).
--
--    Cost note: the batch predicate is an expression, so no index is usable and
--    each batch seq-scans from block 0. stg_asset_tasks is the expensive one:
--    1,945 MB heap against 512 MB shared_buffers, 14 indexes totalling 1,586 MB,
--    and task_name_clean is indexed so every update is non-HOT. Budget ~6 min of
--    scanning and ~6.7 M index insertions for that table alone.
--
--    Expected counts at time of writing (2026-09-23):
--      stg_asset_tasks             479,291 / 2,828,230   (14 indexes, 1,945 MB)
--      stg_asset_tasks_inc         215,728 / 1,280,319   (3 indexes)
--      stg_qa_form                 147,994 /   434,691
--      stg_timer_activities        129,920 /   416,414
--      stg_timer_activities_clean  129,298 /   367,395   (15 indexes)
--      stg_gc_tracker_tasks          6,214 /   794,102
--      stg_user_priorities             581 /    15,225
--      app_timer.entry_additions           5 /       123
--      stg_asset_tasks_gc                  0 /         0  (empty table)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE data_staging._backfill_task_clean_268(
    p_table text, p_src text, p_dst text, p_batch int DEFAULT 50000)
LANGUAGE plpgsql AS $proc$
DECLARE
    v_expr  text := format(
        'BTRIM(regexp_replace(%I, ''^([0-9]+[a-zA-Z]?\.[[:space:]]*)+'', ''''), E'' \t\n\r\f\v'')',
        p_src);
    v_done  bigint := 0;
    v_n     bigint;
BEGIN
    LOOP
        EXECUTE format(
            'UPDATE %s SET %I = %s WHERE ctid IN ('
            '  SELECT ctid FROM %s'
            '  WHERE %I IS NOT NULL AND %I IS DISTINCT FROM %s'
            '  LIMIT %s)',
            p_table, p_dst, v_expr,
            p_table, p_src, p_dst, v_expr, p_batch);
        GET DIAGNOSTICS v_n = ROW_COUNT;
        EXIT WHEN v_n = 0;
        v_done := v_done + v_n;
        COMMIT;
        RAISE NOTICE '268 %: % rows updated so far', p_table, v_done;
    END LOOP;
    RAISE NOTICE '268 %: done, % rows updated', p_table, v_done;
END $proc$;

-- Cheapest first, so an early failure is cheap to diagnose.
CALL data_staging._backfill_task_clean_268('app_timer.entry_additions',              'task',      'task_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_asset_tasks_gc',        'task_name', 'task_name_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_user_priorities',       'task_name', 'task_name_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_gc_tracker_tasks',      'task_name', 'task_name_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_timer_activities',      'task',      'task_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_timer_activities_clean','task',      'task_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_qa_form',               'task',      'task_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_asset_tasks_inc',       'task_name', 'task_name_clean');
CALL data_staging._backfill_task_clean_268('data_staging.stg_asset_tasks',           'task_name', 'task_name_clean');

DROP PROCEDURE data_staging._backfill_task_clean_268(text, text, text, int);

-- data_staging.stg_cop_invoice_forecast is deliberately NOT backfilled: it keeps
-- only task_name_clean, with no raw task_name column, so the stripped revision
-- number is unrecoverable from the stored value. All 1,166 rows currently hold
-- base names. The column is regenerated when the COP invoice forecast pipeline
-- runs again (that pipeline has been broken since 2026-08-08, fix on PR #44),
-- at which point the new rule applies. Tracked as a follow-up.

-- -----------------------------------------------------------------------------
-- 3. Refresh the AI semantic layer. agent.schema_metadata feeds DARA; four rows
--    currently state the exact opposite of the new rule ("trailing numbers
--    removed"), which would make DARA reason and generate SQL incorrectly.
-- -----------------------------------------------------------------------------
UPDATE agent.schema_metadata
SET business_context =
      'Task name with the leading sequence prefix ("7. ", "3F. ") removed. The '
      'trailing revision number is KEPT: "COP Revision Complete 2" is a distinct '
      'task from "COP Revision Complete" and carries its own rate. Use for '
      'grouping and for joining to reference.ref_task_revenue_rates.task_name_norm.',
    updated_at = now()
WHERE column_name IN ('task_clean', 'task_name_clean')
  AND business_context IS NOT NULL
  AND business_context ILIKE '%trailing number%';

DO $meta$
DECLARE stale int;
BEGIN
  SELECT count(*) INTO stale FROM agent.schema_metadata
   WHERE column_name IN ('task_clean','task_name_clean')
     AND business_context ILIKE '%trailing number%removed%';
  IF stale > 0 THEN
    RAISE EXCEPTION '268: % agent.schema_metadata rows still describe the old rule', stale;
  END IF;
  RAISE NOTICE '268: agent.schema_metadata refreshed';
END $meta$;

-- -----------------------------------------------------------------------------
-- 4. Post-backfill assertion: for EVERY backfilled table, no row may be left
--    NULL or still carrying a prefix. All nine are checked -- an earlier draft
--    checked only four, which would have let a silently short-run backfill on
--    one of the others report success.
-- -----------------------------------------------------------------------------
DO $check$
DECLARE
    r    record;
    bad  bigint;
    tot  bigint := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('app_timer.entry_additions',               'task',      'task_clean'),
            ('data_staging.stg_asset_tasks_gc',         'task_name', 'task_name_clean'),
            ('data_staging.stg_user_priorities',        'task_name', 'task_name_clean'),
            ('data_staging.stg_gc_tracker_tasks',       'task_name', 'task_name_clean'),
            ('data_staging.stg_timer_activities',       'task',      'task_clean'),
            ('data_staging.stg_timer_activities_clean', 'task',      'task_clean'),
            ('data_staging.stg_qa_form',                'task',      'task_clean'),
            ('data_staging.stg_asset_tasks_inc',        'task_name', 'task_name_clean'),
            ('data_staging.stg_asset_tasks',            'task_name', 'task_name_clean')
        ) AS t(tbl, src, dst)
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NOT NULL AND (%I IS NULL '
            'OR %I ~ ''^[0-9]+[a-zA-Z]?\.'' '
            'OR %I IS DISTINCT FROM BTRIM(regexp_replace(%I, '
            '''^([0-9]+[a-zA-Z]?\.[[:space:]]*)+'', ''''), E'' \t\n\r\f\v''))',
            r.tbl, r.src, r.dst, r.dst, r.dst, r.src)
        INTO bad;
        IF bad > 0 THEN
            RAISE EXCEPTION '268: % rows in % are NULL, prefixed, or disagree with the rule', bad, r.tbl;
        END IF;
        RAISE NOTICE '268 check %: clean', r.tbl;
        tot := tot + 1;
    END LOOP;
    RAISE NOTICE '268: all % backfilled tables pass', tot;
END $check$;

-- -----------------------------------------------------------------------------
-- 5. Reclaim and re-plan. Autovacuum will NOT cover this:
--    stg_asset_tasks' threshold is 50 + 0.2 * 2,837,973 = 567,645 dead tuples and
--    the backfill makes 479,291, so it never fires (autovacuum_count = 0, the
--    table is kept clean only by the pipeline's manual VACUUM). stg_asset_tasks_inc
--    has never been autovacuumed at all. Stats matter as much as bloat here:
--    110,607 rows leave the 'COP Revision Complete' bucket into 3 variants, and
--    six analytics objects do equality filters on that column, so without an
--    explicit ANALYZE the planner keeps pre-268 MCVs.
--    VACUUM cannot run inside a transaction block; these are separate statements.
-- -----------------------------------------------------------------------------
VACUUM (ANALYZE) data_staging.stg_asset_tasks;
VACUUM (ANALYZE) data_staging.stg_asset_tasks_inc;
VACUUM (ANALYZE) data_staging.stg_timer_activities;
VACUUM (ANALYZE) data_staging.stg_timer_activities_clean;
VACUUM (ANALYZE) data_staging.stg_qa_form;
ANALYZE data_staging.stg_gc_tracker_tasks;
ANALYZE data_staging.stg_user_priorities;
ANALYZE app_timer.entry_additions;

-- -----------------------------------------------------------------------------
-- 6. Refresh the MVs that read the cleaned columns. Verified complete for our
--    schemas via the pg_depend closure: nothing else in analytics reads these
--    columns (mv_timer_day_rollup, mv_daily_report_task_rollup and
--    mv_hr_report_review do NOT). All have a UNIQUE index, so CONCURRENTLY is
--    valid. Run them one at a time, checking the Disk IO budget between each.
--
--    DO NOT refresh analytics.mv_daily_completion_gc. It holds 5,809 rows while
--    its only fact source, data_staging.stg_asset_tasks_gc, is EMPTY (0 rows).
--    Nothing currently refreshes it, which is why it still has data; refreshing
--    it would rebuild it from an empty table and destroy those rows. It also
--    does not need refreshing, because the backfill touches 0 rows there.
--    Pre-existing hazard, recorded here so 268 is not what pulls the trigger.
-- -----------------------------------------------------------------------------
-- REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue;        --  56 MB, 133,943 rows
-- REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue_daily;  --  41 MB, 130,148 rows
-- REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion;     -- 233 MB, 466,718 rows
-- REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_po_issued_status;     -- 104 kB
-- REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_qp_to_po_duration;    --  40 kB

-- =============================================================================
-- ROLLBACK
--
-- Nothing is destroyed by this migration: all 8 backfilled tables keep their
-- source column (task / task_name), so any rule can be recomputed from source
-- forever. The one exception is data_staging.stg_cop_invoice_forecast, which
-- this migration deliberately does not touch.
--
-- It is NOT an exact inverse, though, and should not be called one: steps (a)
-- and (b) in the header were bugs, and the recompute below fixes them again
-- rather than restoring the 119,761 NULL and 2,782 prefixed rows. Post-rollback
-- revenue numbers therefore will not reproduce pre-268 numbers exactly.
--
--   1. Restore the function: re-apply migration 259's CREATE OR REPLACE FUNCTION
--      body verbatim, then verify
--        md5(pg_get_functiondef('data_staging.rebuild_timer_clean()'::regprocedure))
--          = '83b5ef9df31d0b85c634969df116c7d7'
--
--   2. Recompute each column FROM SOURCE with the old rule. Recompute, do not
--      re-strip the derived value: the old rule had TRIM outermost, so
--      re-stripping an already-trimmed value diverges for any name with
--      whitespace before the trailing digits ("Foo 2  " -> old rule gives
--      "Foo 2", re-stripping gives "Foo").
--
--      Recreate the batching helper from section 2 first -- these are the same
--      1.11 M rows and the same Small-tier instance, so an unbatched UPDATE is
--      exactly what section 2 exists to avoid. Then per table, with the right
--      source/destination pair:
--
--        UPDATE data_staging.stg_timer_activities_clean
--           SET task_clean = TRIM(regexp_replace(
--                 regexp_replace(task, '^([0-9]+[a-zA-Z]?\. *)+', ''),
--                 '\s+[0-9]+$', ''))
--         WHERE task IS NOT NULL;
--
--      pairs: app_timer.entry_additions (task -> task_clean),
--             stg_timer_activities (task -> task_clean),
--             stg_timer_activities_clean (task -> task_clean),
--             stg_qa_form (task -> task_clean),
--             stg_user_priorities, stg_asset_tasks, stg_asset_tasks_inc,
--             stg_asset_tasks_gc, stg_gc_tracker_tasks
--               (task_name -> task_name_clean)
--
--   3. Revert the agent.schema_metadata business_context text in section 3.
--   4. VACUUM (ANALYZE) the large tables again (section 5).
--   5. Refresh the five MVs in section 6. Do NOT refresh mv_daily_completion_gc.
--      gap_report.* and scorecard.* are not ours to refresh either way.
-- =============================================================================
