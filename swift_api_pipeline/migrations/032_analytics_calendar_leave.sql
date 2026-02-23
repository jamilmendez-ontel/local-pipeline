-- Migration 032: Analytics view for calendar leave
-- Uses normalized columns where available, falls back to raw parsed values
CREATE OR REPLACE VIEW analytics.v_calendar_leave AS
SELECT
    event_id,
    summary,
    COALESCE(leave_type_normalized, leave_type) AS leave_type,
    leave_type AS leave_type_raw,
    COALESCE(team_normalized, team) AS team,
    team AS team_raw,
    person,
    person_note,
    start_date,
    end_date,
    days,
    is_all_day,
    creator_email,
    event_created,
    event_updated,
    loaded_at
FROM data_staging.stg_calendar_leave;
