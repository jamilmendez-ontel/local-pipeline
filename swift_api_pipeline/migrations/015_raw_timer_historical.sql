-- migrations/015_raw_timer_historical.sql
-- Raw table for historical timer data loaded from Excel (not API)

CREATE TABLE IF NOT EXISTS data_raw.raw_timer_activities_historical (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    run_id      uuid NOT NULL,
    source_file text NOT NULL,
    start_date  date,
    end_date    date,
    run_date    date,
    data        jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_timer_hist_run_id
    ON data_raw.raw_timer_activities_historical(run_id);
