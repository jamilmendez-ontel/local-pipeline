-- 145: per (email, ET work day) timer rollup. union_min = interval-UNION of
-- closed entries (overlaps merged, gaps not counted); open entries excluded
-- from the union but counted (open_count) so the app can suppress variance.
-- Work day = ET calendar day of start_time (matches the drawer's etDayRangeUtc
-- attribution; PH evening shifts + past-midnight OT stay on the start day).
CREATE MATERIALIZED VIEW analytics.mv_timer_day_rollup AS
WITH iv AS (
  SELECT user_email,
         (start_time AT TIME ZONE 'America/New_York')::date AS work_day,
         start_time AS s, end_time AS e
  FROM data_staging.stg_timer_activities_clean
  WHERE user_email IS NOT NULL AND start_time IS NOT NULL
), closed AS (
  SELECT * FROM iv WHERE e IS NOT NULL AND e > s
), marked AS (
  SELECT user_email, work_day, s, e,
         CASE WHEN s > max(e) OVER (PARTITION BY user_email, work_day
              ORDER BY s, e ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
              THEN 1 ELSE 0 END AS nb
  FROM closed
), blocks AS (
  SELECT user_email, work_day, s, e,
         sum(nb) OVER (PARTITION BY user_email, work_day ORDER BY s, e) AS blk
  FROM marked
), merged AS (
  SELECT user_email, work_day, blk, min(s) AS bs, max(e) AS be
  FROM blocks GROUP BY user_email, work_day, blk
), uni AS (
  SELECT user_email, work_day,
         round((sum(EXTRACT(epoch FROM be - bs)) / 60.0)::numeric, 1) AS union_min
  FROM merged GROUP BY user_email, work_day
), counts AS (
  SELECT user_email, work_day, count(*) AS entry_count,
         count(*) FILTER (WHERE e IS NULL) AS open_count,
         min(s) AS first_start
  FROM iv GROUP BY user_email, work_day
)
SELECT c.user_email, c.work_day, COALESCE(u.union_min, 0) AS union_min,
       c.entry_count, c.open_count, c.first_start
FROM counts c LEFT JOIN uni u USING (user_email, work_day);

CREATE UNIQUE INDEX mv_timer_day_rollup_pk
  ON analytics.mv_timer_day_rollup (user_email, work_day);

SELECT cron.schedule('refresh_mv_timer_day_rollup', '*/10 * * * *',
  'REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_day_rollup');

-- Semantic-layer metadata (DATABASE_ARCHITECTURE standard). agent.schema_metadata
-- columns/conflict shape per migration 144 (NOT the schema_name/object_name/object_type
-- template originally sketched in the task brief: actual table has table_name,
-- description, business_context, related_tables and a unique constraint on
-- (schema_name, table_name, column_name); we follow 144's ON CONFLICT DO NOTHING).
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','mv_timer_day_rollup',
   'Per (user_email, ET work day) timer rollup: union_min = merged-interval minutes of closed timer entries (overlaps not double-counted), entry_count, open_count (entries with no stop time, excluded from union_min), first_start. Refreshed every 10 min by pg_cron.',
   'Feeds analytics.v_hr_report_review so the HR report review screen can compare submitted hours against actual clocked time without double-counting overlapping timer entries.',
   ARRAY['data_staging.stg_timer_activities_clean'])
ON CONFLICT DO NOTHING;
