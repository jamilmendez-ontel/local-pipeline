-- Migration 031: Add AI-normalized columns for team and leave_type
ALTER TABLE data_staging.stg_calendar_leave
  ADD COLUMN team_normalized text,
  ADD COLUMN leave_type_normalized text;

CREATE INDEX idx_stg_calendar_leave_team_norm ON data_staging.stg_calendar_leave(team_normalized);
CREATE INDEX idx_stg_calendar_leave_ltype_norm ON data_staging.stg_calendar_leave(leave_type_normalized);
