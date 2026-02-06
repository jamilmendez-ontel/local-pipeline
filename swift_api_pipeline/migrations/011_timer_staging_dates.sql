-- migrations/011_timer_staging_dates.sql
-- Add start_date and end_date columns to stg_timer_activities to match raw table

-- Add start_date column
ALTER TABLE data_staging.stg_timer_activities
ADD COLUMN IF NOT EXISTS start_date DATE;

-- Add end_date column
ALTER TABLE data_staging.stg_timer_activities
ADD COLUMN IF NOT EXISTS end_date DATE;

-- Create index for date range queries
CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_dates
ON data_staging.stg_timer_activities(start_date, end_date);

-- Comment for documentation
COMMENT ON COLUMN data_staging.stg_timer_activities.start_date IS 'Start date of the timer extraction date range';
COMMENT ON COLUMN data_staging.stg_timer_activities.end_date IS 'End date of the timer extraction date range';
