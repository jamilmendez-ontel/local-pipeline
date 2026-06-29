-- migrations/130_analytics_calendar_by_kind.sql
-- Split the conformed data_staging.stg_calendar_events into one serving view per
-- event_kind, mirroring analytics.v_calendar_leave (migration 129). Each view is a
-- thin WHERE event_kind = '<kind>' AND NOT is_deleted slice. Columns are trimmed to
-- those meaningful for the kind (verified against live data 2026-06-29): holiday and
-- birthday carry no person/team (name is in `summary`); `other` drops leave_type /
-- rest_day_of_week (always NULL for non-leave). leave already has its own views.
BEGIN;

DROP VIEW IF EXISTS analytics.v_calendar_holiday;
DROP VIEW IF EXISTS analytics.v_calendar_birthday;
DROP VIEW IF EXISTS analytics.v_calendar_training;
DROP VIEW IF EXISTS analytics.v_calendar_other;

-- Holidays: org-wide all-day events, no person/team. Name is in summary.
CREATE VIEW analytics.v_calendar_holiday AS
SELECT event_id, summary_raw AS summary,
       start_date, end_date, days, is_all_day,
       creator_email, event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'holiday' AND NOT is_deleted;

-- Birthdays: single all-day markers. Celebrant name is in summary (not parsed into person).
CREATE VIEW analytics.v_calendar_birthday AS
SELECT event_id, summary_raw AS summary,
       start_date, is_all_day,
       event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'birthday' AND NOT is_deleted;

-- Training: team/person kept (meaningful for a session) though currently sparse.
CREATE VIEW analytics.v_calendar_training AS
SELECT event_id, summary_raw AS summary,
       team, team_normalized, team_level,
       person, person_normalized, emp_id, person_match_source,
       start_date, end_date, days, is_all_day,
       creator_email, event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'training' AND NOT is_deleted;

-- Other: catch-all. person/team populated; leave_type/rest_day_of_week omitted (always NULL here).
CREATE VIEW analytics.v_calendar_other AS
SELECT event_id, summary_raw AS summary,
       team, team_normalized, team_level,
       person, person_normalized, emp_id, person_match_source,
       person_note, person_note_normalized,
       start_date, end_date, days, is_all_day,
       creator_email, event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'other' AND NOT is_deleted;

COMMIT;
