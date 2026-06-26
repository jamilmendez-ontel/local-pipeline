-- migrations/129_v_calendar_leave_normalized.sql
-- Expose the Phase 2 normalization columns through the HR-facing views.
-- Recreates the views from migration 125 with the added columns.
BEGIN;

DROP VIEW IF EXISTS analytics.v_calendar_leave_daily;
DROP VIEW IF EXISTS analytics.v_calendar_leave;

CREATE VIEW analytics.v_calendar_leave AS
SELECT event_id, summary_raw AS summary, leave_type, leave_type_normalized,
       team, team_normalized, team_level, person, person_normalized, emp_id,
       person_match_source, person_note, person_note_normalized, rest_day_of_week,
       start_date, end_date, days, is_all_day, creator_email,
       event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'leave' AND NOT is_deleted;

CREATE VIEW analytics.v_calendar_leave_daily AS
SELECT e.*, gs::date AS leave_date
FROM analytics.v_calendar_leave e
CROSS JOIN LATERAL generate_series(e.start_date, e.end_date, interval '1 day') gs;

COMMIT;
