-- 124: Phase 1 of the calendar pipeline restructure. Creates the conformed
-- stg_calendar_events table and the agent.calendar_summary_parse cache,
-- beside the live stg_calendar_leave (which keeps serving until cutover).
-- Non-destructive: no rename, no view change (those are migration 125).

CREATE TABLE IF NOT EXISTS data_staging.stg_calendar_events (
    event_id              text PRIMARY KEY,
    ical_uid              text,
    summary_raw           text,
    event_kind            text NOT NULL DEFAULT 'other',
    leave_type            text,
    leave_type_normalized text,
    team                  text,
    team_normalized       text,
    person                text,
    person_note           text,
    rest_day_of_week      text,
    start_date            date,
    end_date              date,
    days                  integer,
    is_all_day            boolean,
    creator_email         text,
    event_created         timestamptz,
    event_updated         timestamptz,
    parse_source          text,
    parse_confidence      real,
    needs_review          boolean NOT NULL DEFAULT false,
    is_deleted            boolean NOT NULL DEFAULT false,
    deleted_at            timestamptz,
    run_id                text,
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    parsed_at             timestamptz,
    CONSTRAINT chk_event_kind
        CHECK (event_kind IN ('leave','holiday','birthday','training','other')),
    CONSTRAINT chk_leave_norm_only_leave
        CHECK (leave_type_normalized IS NULL OR event_kind = 'leave'),
    CONSTRAINT chk_restday_only_leave
        CHECK (rest_day_of_week IS NULL OR event_kind = 'leave'),
    CONSTRAINT chk_restday_value
        CHECK (rest_day_of_week IS NULL OR
               rest_day_of_week IN ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'))
);

CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_kind
    ON data_staging.stg_calendar_events (event_kind);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_start_date
    ON data_staging.stg_calendar_events (start_date);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_person
    ON data_staging.stg_calendar_events (person);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_needs_review
    ON data_staging.stg_calendar_events (needs_review) WHERE needs_review;

CREATE TABLE IF NOT EXISTS agent.calendar_summary_parse (
    summary_key       text PRIMARY KEY,   -- whitespace-collapsed summary_raw
    event_kind        text NOT NULL,
    leave_type        text,
    team              text,
    person            text,
    person_note       text,
    rest_day_of_week  text,
    confidence        real,
    parse_source      text NOT NULL,      -- 'deterministic' | 'ai'
    needs_review      boolean NOT NULL DEFAULT false,
    model             text,
    prompt_version    text,
    extracted_at      timestamptz NOT NULL DEFAULT now()
);

-- schema_metadata uses (schema_name, table_name, description); description is NOT NULL.
INSERT INTO agent.schema_metadata (schema_name, table_name, description)
VALUES
  ('data_staging','stg_calendar_events',
   'Conformed calendar events (leave/holiday/birthday/training/other) with soft-delete. Phase 1 restructure of stg_calendar_leave.'),
  ('agent','calendar_summary_parse',
   'Persisted parse cache keyed on calendar summary string; makes extraction deterministic across runs.')
ON CONFLICT DO NOTHING;
