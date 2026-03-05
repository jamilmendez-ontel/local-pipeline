-- Analytics view for timer discrepancies (ET timestamps)
CREATE OR REPLACE VIEW analytics.v_timer_discrepancies AS
SELECT
    id,
    submission_timestamp AT TIME ZONE 'America/New_York' AS submission_timestamp_et,
    email_address,
    ontel_email,
    shift_schedule,
    discrepancy_date,
    asset_name,
    task_name,
    correct_duration_minutes,
    description,
    row_number,
    loaded_at AT TIME ZONE 'America/New_York' AS loaded_at_et
FROM data_staging.stg_timer_discrepancies
ORDER BY submission_timestamp DESC;

-- Schema metadata for DARA
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description)
VALUES
    ('analytics', 'v_timer_discrepancies', NULL,
     'Timer error/discrepancy reports submitted by technicians via Google Form. Each row is a form submission reporting incorrect timer duration on a task.'),
    ('analytics', 'v_timer_discrepancies', 'submission_timestamp_et',
     'When the form was submitted (Eastern Time)'),
    ('analytics', 'v_timer_discrepancies', 'email_address',
     'Google account email of the submitter'),
    ('analytics', 'v_timer_discrepancies', 'ontel_email',
     'Self-reported Ontel email address of the submitter'),
    ('analytics', 'v_timer_discrepancies', 'shift_schedule',
     'Shift when the error occurred: Dayshift (6PM-3AM EST) or Nightshift (9AM-6PM EST)'),
    ('analytics', 'v_timer_discrepancies', 'discrepancy_date',
     'Date when the timer error/discrepancy occurred'),
    ('analytics', 'v_timer_discrepancies', 'asset_name',
     'Asset/site name where the timer error occurred'),
    ('analytics', 'v_timer_discrepancies', 'task_name',
     'Task name where the timer error occurred'),
    ('analytics', 'v_timer_discrepancies', 'correct_duration_minutes',
     'The correct duration in minutes as reported by the technician (0 means timer should be removed)'),
    ('analytics', 'v_timer_discrepancies', 'description',
     'Free-text description of what went wrong with the timer')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description;
