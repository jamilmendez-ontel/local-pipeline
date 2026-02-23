-- Migration 030: Calendar leave tables (raw + staging)
-- Source: Google Calendar "Leave/RD/Weekend Work Calendar"
-- Summary format: "Type of leave - Group - Person"

-- ============================================================
-- RAW TABLE
-- ============================================================
CREATE TABLE data_raw.raw_calendar_leave (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      text NOT NULL,
    event_id    text NOT NULL,
    data        jsonb NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_raw_calendar_leave_run_id ON data_raw.raw_calendar_leave(run_id);
CREATE INDEX idx_raw_calendar_leave_event_id ON data_raw.raw_calendar_leave(event_id);

-- ============================================================
-- STAGING TABLE
-- ============================================================
CREATE TABLE data_staging.stg_calendar_leave (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        text NOT NULL UNIQUE,
    summary         text,
    leave_type      text,
    team            text,
    person          text,
    person_note     text,
    start_date      date NOT NULL,
    end_date        date NOT NULL,
    days            int NOT NULL DEFAULT 1,
    is_all_day      boolean NOT NULL DEFAULT true,
    creator_email   text,
    event_created   timestamptz,
    event_updated   timestamptz,
    run_id          text NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_stg_calendar_leave_leave_type ON data_staging.stg_calendar_leave(leave_type);
CREATE INDEX idx_stg_calendar_leave_person ON data_staging.stg_calendar_leave(person);
CREATE INDEX idx_stg_calendar_leave_team ON data_staging.stg_calendar_leave(team);
CREATE INDEX idx_stg_calendar_leave_start_date ON data_staging.stg_calendar_leave(start_date);
