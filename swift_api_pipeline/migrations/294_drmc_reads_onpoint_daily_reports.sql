-- 294: DRMC reads OnPoint daily reports (from 2026-11-01) through its existing views.
-- NOT YET APPLIED.
--
-- Why: the team files daily reports and clocks in on OnPoint from November 2026
-- (Swift clock-in people per week: 96 on Oct 26 -> 26 on Nov 16). Migration 293
-- lands OnPoint reports in data_staging.stg_onpoint_daily_reports; this one lays
-- them over the Swift report row of the same person and day, so the DRMC pages
-- (DR Approval browse, HR Review, Hours Analysis, Team Review, packs) show the
-- OnPoint report, declared hours, clock-in and submit time with no app change.
-- Scope (Jamil 2026-11-20): the daily report and the declared work hours.
-- Approver / approved-at and attachments are NOT carried over.
--
-- Mapping: OnPoint user -> email (stg_onpoint_users) -> emp_id
-- (reference.ref_employee_emails, newest last_seen, else ref_employees.email)
-- -> the Swift stg_daily_reports row on (emp_id, work_date). Swift pre-creates
-- one row per person per day; for 2026-11-01..20 every mapped OnPoint report had
-- one (1,339 reports, 4 unmapped = 1 user not on the roster).
-- Precedence: a FILED OnPoint report (has hours or a submit time) wins over Swift
-- for that person-day. OnPoint "approved" shells with no hours and no submit time
-- (141, bulk-created 2026-11-12, leave-like) do not override Swift.
-- OnPoint rejected -> 'in_progress' (sent back to the member).
--
-- Objects:
--  * analytics.v_onpoint_daily_reports (new): one filed report per emp_id + work_date.
--  * analytics.v_daily_report_approvals: CREATE OR REPLACE, column list kept and 5
--    columns APPENDED (source, first_clock_in, submitted_on, onpoint_undertime,
--    onpoint_report_id). For OnPoint person-days task_status, total_hours,
--    clock_in_et, clock_in_late_minutes and submitted_on_et come from OnPoint, and
--    is_awaiting_approval / no_approver_flag are false, pending_wait_days NULL:
--    approval happens in OnPoint, and this keeps the rows out of DRMC's Swift
--    approve queue and its server-side approve check (both key on
--    is_awaiting_approval). is_tardy also honors a "007 - UT" line in the notes.
--  * analytics.mv_hr_report_review: rebuilt reading b.first_clock_in and
--    b.submitted_on (it read the Swift rollup clock-in and Swift submitted_on
--    directly), has_undertime ORs the OnPoint notes, source appended. A
--    materialized view cannot be redefined in place and v_hr_report_review must
--    NEVER be dropped (hr_review_page returns its rowtype, mig 207), so: build
--    mv_hr_report_review_new, CREATE OR REPLACE v_hr_report_review onto it (same
--    columns), drop the old MV (that view was its only dependent), rename _new
--    back, recreate the 3 indexes under their old names and the grants.
--    refresh_hr_report_review_safe() names the MV, so the 5-min pg_cron chain
--    (jobid 9) keeps working unchanged.
--  * analytics.v_daily_report_lines (new): what DRMC's report drawer lists. Swift
--    requirement lines, or the one OnPoint report (notes + hours) in their place.
--    Read by ontel-people (separate PR); until then the drawer shows Swift lines.
--
-- Guard: aborts unless the live view texts still hash to what this was generated
-- from (out/m294/gen294.py + build294.py, gitignored).
--
-- Rollback: out/m294/rollback_294.sql (pre-294 bodies, same build-then-swap for
-- the MV; the 5 appended view columns stay, since CREATE OR REPLACE cannot drop
-- columns, but read Swift-only values).

SET LOCAL lock_timeout = '15s';
SET LOCAL statement_timeout = '600s';

DO $guard$
BEGIN
  IF md5(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass, true)) <> 'b6bad75a761d5277bad70fb2ef4c4b13' THEN
    RAISE EXCEPTION '294: live v_daily_report_approvals changed since this migration was generated';
  END IF;
  IF md5(pg_get_viewdef('analytics.mv_hr_report_review'::regclass, true)) <> 'ba108214680244c343975a3fcba93218' THEN
    RAISE EXCEPTION '294: live mv_hr_report_review changed since this migration was generated';
  END IF;
END
$guard$;

-- 1. OnPoint report per person-day, mapped to emp_id ------------------------
CREATE VIEW analytics.v_onpoint_daily_reports AS
SELECT DISTINCT ON (m.emp_id, r.report_date)
       m.emp_id,
       r.report_date AS work_date,
       r.report_id,
       r.user_id,
       u.email,
       CASE r.status WHEN 'rejected' THEN 'in_progress' ELSE r.status END AS status,
       r.status AS onpoint_status,
       r.self_reported_hours AS hours,
       r.worked_minutes,
       r.time_in,
       r.time_out,
       r.submitted_at,
       r.notes,
       r.updated_at
FROM data_staging.stg_onpoint_daily_reports r
JOIN data_staging.stg_onpoint_users u ON u.user_id = r.user_id
CROSS JOIN LATERAL (
  SELECT COALESCE(
           (SELECT ea.emp_id FROM reference.ref_employee_emails ea
             WHERE lower(ea.email) = u.email ORDER BY ea.last_seen DESC NULLS LAST LIMIT 1),
           (SELECT re.emp_id FROM reference.ref_employees re
             WHERE lower(re.email) = u.email ORDER BY re.effective_date DESC LIMIT 1)) AS emp_id
) m
WHERE m.emp_id IS NOT NULL
  AND r.report_date >= DATE '2026-11-01'
  AND (r.self_reported_hours IS NOT NULL OR r.submitted_at IS NOT NULL)
ORDER BY m.emp_id, r.report_date, r.submitted_at DESC NULLS LAST, r.updated_at DESC NULLS LAST;

COMMENT ON VIEW analytics.v_onpoint_daily_reports IS
  'Filed OnPoint daily reports from 2026-11-01, one per emp_id + work_date (mig 294). Overlays the Swift report row of the same person-day in v_daily_report_approvals.';

-- 2. approvals view ----------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 SELECT b.emp_id,
    b.employee_name,
    b.nickname,
    b.email,
    b."position",
    b.carrier,
    b.carrier_group,
    b.cluster,
    b.division,
    b.sub_division,
    b.employment_status,
    b.work_date,
    b.task_did,
    b.task_status,
    b.asset_name,
    b.milestone,
    b.req_count,
    b.total_hours,
    b.clock_in_et,
    b.assigned_approver,
    b.submitted_on_et,
    b.approved_on_et,
    b.approved_by,
    b.is_awaiting_approval,
    b.pending_wait_days,
    b.approval_latency_days,
    b.no_approver_flag,
    b.shift_time_in_pht,
    b.clock_in_late_minutes,
    b.timed_hours,
    b.open_timer_count,
    b.has_timer_history,
    b.work_dow,
    COALESCE(b.clock_in_late_minutes > 30 AND b.task_status IS DISTINCT FROM 'cancelled'::text AND (b.work_dow <> ALL (ARRAY[0, 6])) AND NOT (EXISTS ( SELECT 1
           FROM data_staging.stg_daily_report_hours h
          WHERE h.task_did = b.task_did AND h.work_description ~* '^\s*007\s*-?\s*UT\M'::text)) AND NOT b.onpoint_undertime, false) AS is_tardy,
        CASE
            WHEN b.timed_hours IS NOT NULL AND b.open_timer_count = 0 AND b.total_hours IS NOT NULL THEN round(GREATEST(b.total_hours - 1::numeric, 0::numeric) - b.timer_union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN b.timed_hours IS NOT NULL AND b.open_timer_count = 0 AND b.total_hours IS NOT NULL AND GREATEST(b.total_hours - 1::numeric, 0::numeric) > 0::numeric THEN round(100.0 * b.timer_union_min / 60.0 / GREATEST(b.total_hours - 1::numeric, 0::numeric), 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.source,
    b.first_clock_in,
    b.submitted_on,
    b.onpoint_undertime,
    b.onpoint_report_id
   FROM ( WITH today_et AS (
                 SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
                ), today_pht AS (
                 SELECT (now() AT TIME ZONE 'Asia/Manila'::text)::date AS d
                )
         SELECT t.emp_id,
            COALESCE(
                CASE
                    WHEN t.asset_name IS NOT NULL AND t.emp_id IS NOT NULL AND "right"(t.asset_name, length(t.emp_id) + 1) = ('_'::text || t.emp_id) THEN "left"(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
                    ELSE NULL::text
                END, e.full_name, r.attendance_user_name) AS employee_name,
            e.nickname,
            e.email,
            e."position",
            e.carrier,
            e.carrier_group,
            e.cluster,
            e.division,
            e.sub_division,
            e.employment_status,
            t.work_date,
            t.task_did,
                CASE
                    WHEN op.emp_id IS NOT NULL THEN op.status
                    WHEN t.task_status = 'approved'::text THEN t.task_status
                    WHEN la.task_did IS NOT NULL THEN 'approved'::text
                    ELSE t.task_status
                END AS task_status,
            t.asset_name,
            t.milestone,
            COALESCE(r.req_count, 0::bigint) AS req_count,
                CASE
                    WHEN op.emp_id IS NOT NULL THEN op.hours
                    WHEN t.task_status = 'cancelled'::text THEN 0::numeric
                    ELSE ( SELECT
                            CASE
                                WHEN count(*) = 0 THEN NULL::numeric
                                ELSE COALESCE(sum(h.hours_worked) FILTER (WHERE h.req_status IS DISTINCT FROM 'cancelled'::text), 0::numeric)
                            END AS "coalesce"
                       FROM data_staging.stg_daily_report_hours h
                      WHERE h.task_did = t.task_did)
                END AS total_hours,
            (eff.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
            t.assigned_approver,
            (eff.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
            (COALESCE(t.approved_on,
                CASE
                    WHEN la.swift_status = 'approved'::text THEN la.approved_at
                    ELSE NULL::timestamp with time zone
                END) AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
            COALESCE(t.approved_by,
                CASE
                    WHEN la.task_did IS NOT NULL AND la.swift_status = 'approved'::text THEN COALESCE(appr.display_name, la.approver_email)
                    ELSE NULL::text
                END) AS approved_by,
            op.emp_id IS NULL AND t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL AS is_awaiting_approval,
                CASE
                    WHEN op.emp_id IS NULL AND t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL THEN (( SELECT today_et.d
                       FROM today_et)) - (eff.submitted_on AT TIME ZONE 'America/New_York'::text)::date
                    ELSE NULL::integer
                END AS pending_wait_days,
                CASE
                    WHEN COALESCE(t.approved_on,
                    CASE
                        WHEN la.swift_status = 'approved'::text THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) IS NOT NULL AND eff.submitted_on IS NOT NULL THEN (COALESCE(t.approved_on,
                    CASE
                        WHEN la.swift_status = 'approved'::text THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) AT TIME ZONE 'America/New_York'::text)::date - (eff.submitted_on AT TIME ZONE 'America/New_York'::text)::date
                    ELSE NULL::integer
                END AS approval_latency_days,
            op.emp_id IS NULL AND t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL AND t.assigned_approver IS NULL AS no_approver_flag,
            e.shift_time_in_pht,
                CASE
                    WHEN eff.first_clock_in IS NOT NULL AND e.shift_time_in_pht ~* '^\s*\d{1,2}(:\d{2})?\s*(AM|PM)\s*$'::text THEN mod(floor(EXTRACT(epoch FROM (eff.first_clock_in AT TIME ZONE 'Asia/Manila'::text)::time without time zone - e.shift_time_in_pht::time without time zone) / 60::numeric)::integer + 2160, 1440) - 720
                    ELSE NULL::integer
                END AS clock_in_late_minutes,
                CASE
                    WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
                    ELSE NULL::numeric
                END AS timed_hours,
            COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
            th.person_key IS NOT NULL AS has_timer_history,
            EXTRACT(dow FROM t.work_date)::smallint AS work_dow,
            tm.union_min AS timer_union_min,
                CASE
                    WHEN op.emp_id IS NOT NULL THEN 'onpoint'::text
                    ELSE 'swift'::text
                END AS source,
            eff.first_clock_in,
            eff.submitted_on,
            COALESCE(op.notes ~* '^\s*007\s*-?\s*UT\M'::text, false) AS onpoint_undertime,
            op.report_id AS onpoint_report_id
           FROM data_staging.stg_daily_reports t
             LEFT JOIN analytics.mv_daily_report_task_rollup r ON t.task_did = r.task_did
             LEFT JOIN analytics.v_onpoint_daily_reports op ON op.emp_id = t.emp_id AND op.work_date = t.work_date
             CROSS JOIN LATERAL ( SELECT
                        CASE
                            WHEN op.emp_id IS NOT NULL THEN COALESCE(op.time_in, r.first_clock_in)
                            ELSE r.first_clock_in
                        END AS first_clock_in,
                        CASE
                            WHEN op.emp_id IS NOT NULL THEN COALESCE(op.submitted_at, t.submitted_on)
                            ELSE t.submitted_on
                        END AS submitted_on) eff
             LEFT JOIN LATERAL ( SELECT l.task_did,
                    l.approver_email,
                    l.swift_status,
                    l.approved_at
                   FROM app_hr.report_approval_log l
                  WHERE l.task_did = t.task_did AND l.ok AND l.approved_at >= (now() - '30 days'::interval)
                 LIMIT 1) la ON true
             LEFT JOIN LATERAL ( SELECT COALESCE(( SELECT "left"(dr.asset_name, length(dr.asset_name) - length(dr.emp_id) - 1) AS "left"
                           FROM data_staging.stg_daily_reports dr
                          WHERE dr.emp_id = re2.emp_id AND dr.asset_name IS NOT NULL AND "right"(dr.asset_name, length(dr.emp_id) + 1) = ('_'::text || dr.emp_id) AND dr.work_date IS NOT NULL
                          ORDER BY dr.work_date DESC
                         LIMIT 1), re2.full_name) AS display_name
                   FROM reference.ref_employees re2
                  WHERE re2.emp_id = (( SELECT ea.emp_id
                           FROM reference.ref_employee_emails ea
                          WHERE ea.email = lower(la.approver_email)
                          ORDER BY ea.last_seen DESC
                         LIMIT 1)) OR lower(re2.email) = lower(la.approver_email)
                  ORDER BY re2.effective_date DESC
                 LIMIT 1) appr ON la.task_did IS NOT NULL AND la.swift_status = 'approved'::text
             LEFT JOIN LATERAL ( SELECT re.full_name,
                    re.nickname,
                    re.email,
                    re."position",
                    re.carrier,
                    re.carrier_group,
                    re.cluster,
                    re.division,
                    re.sub_division,
                    re.employment_status,
                    re.shift_time_in_pht
                   FROM reference.ref_employees re
                  WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
                  ORDER BY re.effective_date DESC
                 LIMIT 1) e ON true
             LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = t.emp_id AND tm.work_day = t.work_date
             LEFT JOIN LATERAL ( SELECT m2.person_key
                   FROM analytics.mv_timer_day_rollup m2
                  WHERE m2.person_key = t.emp_id
                 LIMIT 1) th ON true
          WHERE t.work_date IS NOT NULL AND t.work_date <= (( SELECT today_pht.d
                   FROM today_pht))) b;

-- 3. HR review MV: build new, repoint the view, swap ------------------------
CREATE MATERIALIZED VIEW analytics.mv_hr_report_review_new AS
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
    b.first_clock_in IS NOT NULL AS has_time_in,
    round(EXTRACT(epoch FROM b.submitted_on - b.first_clock_in) / 3600.0, 1) AS filing_lag_hours,
    b.first_clock_in +
        CASE
            WHEN EXTRACT(dow FROM b.work_date) = 5::numeric THEN '61:00:00'::interval
            ELSE '49:00:00'::interval
        END AS deadline_at,
    b.submitted_on IS NOT NULL AND b.first_clock_in IS NOT NULL AND b.submitted_on >= (b.first_clock_in +
        CASE
            WHEN EXTRACT(dow FROM b.work_date) = 5::numeric THEN '61:00:00'::interval
            ELSE '49:00:00'::interval
        END) AS is_late_filing,
    COALESCE(b.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(b.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(b.first_clock_in, tm.first_start) +
        CASE
            WHEN EXTRACT(dow FROM b.work_date) = 5::numeric THEN '61:00:00'::interval
            ELSE '49:00:00'::interval
        END) AS is_missing_report,
    COALESCE(b.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(b.first_clock_in, tm.first_start) +
        CASE
            WHEN EXTRACT(dow FROM b.work_date) = 5::numeric THEN '61:00:00'::interval
            ELSE '49:00:00'::interval
        END) AS is_matured,
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
    (EXISTS ( SELECT 1
           FROM data_staging.stg_daily_report_hours h
          WHERE h.task_did = b.task_did AND h.work_description ~* '^\s*007\s*-?\s*UT\M'::text)) OR b.onpoint_undertime AS has_undertime,
    b.source
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true
     LEFT JOIN LATERAL ( SELECT min(x.st) AS first_task_start
           FROM UNNEST(tm.start_times, tm.end_times) x(st, en)
          WHERE x.en IS NULL OR x.en > b.first_clock_in) fts ON tm.person_key IS NOT NULL AND b.first_clock_in IS NOT NULL
     LEFT JOIN LATERAL ( SELECT g.long_gap_count,
            g.long_gap_minutes
           FROM analytics.long_gap_stats(tm.block_starts, tm.block_ends, b.first_clock_in, 21::numeric, b.first_clock_in + make_interval(secs => (b.total_hours * 3600::numeric)::double precision)) g(long_gap_count, long_gap_minutes)) gap ON tm.person_key IS NOT NULL AND fts.first_task_start IS NOT NULL AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
     LEFT JOIN LATERAL ( SELECT count(*) FILTER (WHERE st.st < b.first_clock_in) AS early_task_count,
            count(*) FILTER (WHERE st.st > (b.first_clock_in + make_interval(secs => (b.total_hours * 3600::numeric)::double precision))) AS late_task_count
           FROM unnest(tm.start_times) st(st)) ow ON tm.person_key IS NOT NULL AND b.first_clock_in IS NOT NULL AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric
WITH DATA;

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
   FROM analytics.mv_hr_report_review_new m;

DROP MATERIALIZED VIEW analytics.mv_hr_report_review;
ALTER MATERIALIZED VIEW analytics.mv_hr_report_review_new RENAME TO mv_hr_report_review;
CREATE UNIQUE INDEX mv_hr_report_review_pk ON analytics.mv_hr_report_review USING btree (task_did);
CREATE INDEX idx_mv_hr_review_work_date ON analytics.mv_hr_report_review USING btree (work_date, task_did);
CREATE INDEX idx_mv_hr_review_carrier_date ON analytics.mv_hr_report_review USING btree (carrier_group, work_date);
REVOKE ALL ON analytics.mv_hr_report_review FROM PUBLIC, anon, authenticated;
GRANT ALL ON analytics.mv_hr_report_review TO service_role;
GRANT SELECT ON analytics.mv_hr_report_review TO my_timers_owner;

-- 4. report lines for DRMC's drawer ------------------------------------------
CREATE VIEW analytics.v_daily_report_lines AS
SELECT h.task_did, h.emp_id, h.work_date, h.req_id, h.work_description, h.hours_worked,
       h.req_status, h.file_uploaded_count, 'swift'::text AS source
FROM data_staging.stg_daily_report_hours h
WHERE NOT EXISTS (SELECT 1 FROM analytics.v_onpoint_daily_reports op
                  WHERE op.emp_id = h.emp_id AND op.work_date = h.work_date)
UNION ALL
SELECT t.task_did, t.emp_id, t.work_date, 'onpoint:' || op.report_id::text AS req_id,
       op.notes AS work_description, op.hours AS hours_worked, op.status AS req_status,
       0 AS file_uploaded_count, 'onpoint'::text AS source
FROM analytics.v_onpoint_daily_reports op
JOIN data_staging.stg_daily_reports t ON t.emp_id = op.emp_id AND t.work_date = op.work_date;

COMMENT ON VIEW analytics.v_daily_report_lines IS
  'Daily report lines for DRMC (mig 294): Swift requirement lines, or the OnPoint report (notes + hours) for the person-days OnPoint covers.';

REVOKE ALL ON analytics.v_onpoint_daily_reports, analytics.v_daily_report_lines FROM PUBLIC, anon, authenticated;
GRANT SELECT ON analytics.v_onpoint_daily_reports, analytics.v_daily_report_lines TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
SELECT s, t, NULL, d FROM (VALUES
  ('analytics', 'v_onpoint_daily_reports', 'Filed OnPoint daily reports per emp_id + work_date from 2026-11-01; overlays Swift in the DRMC views (mig 294).'),
  ('analytics', 'v_daily_report_lines', 'DRMC report-drawer lines: Swift requirement lines or the OnPoint report (mig 294).')
) v(s, t, d)
WHERE NOT EXISTS (SELECT 1 FROM agent.schema_metadata m
                  WHERE m.schema_name = v.s AND m.table_name = v.t AND m.column_name IS NULL);

NOTIFY pgrst, 'reload schema';
