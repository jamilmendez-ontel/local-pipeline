-- 170: rekey the timer day rollup from (user_email, work_day) to a resolved
-- person key, so timer history follows the PERSON (emp_id via the migration
-- 167/169 alias map) instead of whatever email the entry was logged under.
-- A member whose email changes (e.g. Jehane Ong -> Abilay, live case) keeps
-- one unbroken timer history; v_hr_report_review joins timers by emp_id.
--
-- person_key = emp_id when the entry's email resolves through
-- reference.ref_employee_emails, else 'email:<address>' (kept visible for the
-- new watcher view instead of silently dropped).
--
-- SWAP ORDER MATTERS: analytics.hr_review_page RETURNS SETOF
-- v_hr_report_review, so the view must never be dropped. Sequence: create the
-- new MV -> CREATE OR REPLACE the view onto it -> unschedule + drop the old
-- MV -> rename the new MV/index to the old names -> re-schedule the refresh.

-- 1. New person-keyed MV (same interval-union semantics as migration 145).
CREATE MATERIALIZED VIEW analytics.mv_timer_day_rollup_new AS
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
         round((sum(EXTRACT(epoch FROM be - bs)) / 60.0)::numeric, 1) AS union_min
  FROM merged GROUP BY person_key, work_day
), counts AS (
  SELECT person_key, max(emp_id) AS emp_id, work_day,
         count(*) AS entry_count,
         count(*) FILTER (WHERE e IS NULL) AS open_count,
         min(s) AS first_start
  FROM resolved GROUP BY person_key, work_day
)
SELECT c.person_key, c.emp_id, c.work_day, COALESCE(u.union_min, 0) AS union_min,
       c.entry_count, c.open_count, c.first_start
FROM counts c LEFT JOIN uni u USING (person_key, work_day);

CREATE UNIQUE INDEX mv_timer_day_rollup_new_pk
  ON analytics.mv_timer_day_rollup_new (person_key, work_day);

-- 2. Re-point the review view: timers join by emp_id now. Only the tm/th
-- joins and their null-signals change; column list and order are identical.
CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
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
    r.first_clock_in + '48:00:00'::interval AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL AND t.submitted_on > (r.first_clock_in + '48:00:00'::interval) AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() > (COALESCE(r.first_clock_in, tm.first_start) + '48:00:00'::interval) AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() > (COALESCE(r.first_clock_in, tm.first_start) + '48:00:00'::interval) AS is_matured,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    COALESCE(tm.entry_count, 0::bigint) AS timer_entry_count,
    th.person_key IS NOT NULL AS has_timer_history,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL THEN round(b.total_hours - tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL AND b.total_hours > 0::numeric THEN round(100.0 * tm.union_min / 60.0 / b.total_hours, 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.shift_time_in_pht,
    b.clock_in_late_minutes
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup_new tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup_new m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true;

-- 3. Retire the email-keyed MV, take over its name and refresh schedule.
SELECT cron.unschedule('refresh_mv_timer_day_rollup');
DROP MATERIALIZED VIEW analytics.mv_timer_day_rollup;
ALTER MATERIALIZED VIEW analytics.mv_timer_day_rollup_new RENAME TO mv_timer_day_rollup;
ALTER INDEX analytics.mv_timer_day_rollup_new_pk RENAME TO mv_timer_day_rollup_pk;
SELECT cron.schedule('refresh_mv_timer_day_rollup', '*/10 * * * *',
  'REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_day_rollup');

-- 4. Watcher surface: timer identities that resolve to no employee. Feeds a
-- future roster-gap-watcher-style alert; until then it is queryable directly.
CREATE OR REPLACE VIEW analytics.v_unmatched_timer_emails AS
SELECT replace(person_key, 'email:', '') AS user_email,
       max(work_day)                     AS last_seen_day,
       sum(entry_count)                  AS entries,
       round(sum(union_min) / 60.0, 1)   AS hours
FROM analytics.mv_timer_day_rollup
WHERE person_key LIKE 'email:%'
  AND work_day >= current_date - 30
GROUP BY 1;

REVOKE ALL ON analytics.v_unmatched_timer_emails FROM anon, authenticated;
GRANT SELECT ON analytics.v_unmatched_timer_emails TO service_role;

-- 5. Semantic metadata.
UPDATE agent.schema_metadata
SET description = 'Per (person_key, ET work day) timer rollup: person_key = emp_id when the entry email resolves via reference.ref_employee_emails, else ''email:<address>''. union_min = merged-interval minutes of closed timer entries (overlaps not double-counted), emp_id (null when unresolved), entry_count, open_count (open entries excluded from union_min), first_start. Refreshed every 10 min by pg_cron.'
WHERE schema_name = 'analytics' AND table_name = 'mv_timer_day_rollup' AND column_name IS NULL;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','v_unmatched_timer_emails',
   'Timer identities from the last 30 days whose email resolves to NO employee in reference.ref_employee_emails: user_email, last_seen_day, entries, hours. Empty when person resolution is healthy.',
   'Watcher surface for the email-alias system: a row here means someone''s timer hours are not attached to any emp_id (new hire missing from roster, typo, or an email change neither the HR sheet nor a Swift submission has registered yet).',
   ARRAY['analytics.mv_timer_day_rollup','reference.ref_employee_emails'])
ON CONFLICT DO NOTHING;
