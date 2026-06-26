-- migrations/128_calendar_events_normalize_columns.sql
-- Phase 2 normalization columns on the conformed staging table + the person
-- match cache (mirrors agent.calendar_summary_parse).
BEGIN;

ALTER TABLE data_staging.stg_calendar_events
    ADD COLUMN IF NOT EXISTS person_normalized      text,
    ADD COLUMN IF NOT EXISTS emp_id                 text,
    ADD COLUMN IF NOT EXISTS person_match_source    text,
    ADD COLUMN IF NOT EXISTS team_level             text,
    ADD COLUMN IF NOT EXISTS person_note_normalized text;

CREATE TABLE IF NOT EXISTS agent.calendar_person_match (
    person_raw        text NOT NULL,
    team_raw          text NOT NULL DEFAULT '',
    emp_id            text,
    person_normalized text,
    confidence        numeric,
    match_source      text,
    resolved_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (person_raw, team_raw)
);

COMMIT;
