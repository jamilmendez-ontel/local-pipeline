-- Migration 033: Daily exploded view for calendar leave
-- One row per person per day on leave (multi-day events expanded)
CREATE OR REPLACE VIEW analytics.v_calendar_leave_daily AS
SELECT
    event_id,
    d::date AS leave_date,
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
    summary
FROM data_staging.stg_calendar_leave,
     generate_series(start_date, end_date, interval '1 day') AS d;
