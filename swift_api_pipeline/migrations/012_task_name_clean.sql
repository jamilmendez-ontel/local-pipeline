-- migrations/012_task_name_clean.sql
-- Add task_name_clean column to stg_asset_tasks
-- Removes sequence numbers from beginning (e.g., "1. ", "2. ")
-- and revision numbers from end (e.g., " 2", " 3")

-- Add the column
ALTER TABLE data_staging.stg_asset_tasks
ADD COLUMN IF NOT EXISTS task_name_clean TEXT;

-- Create index for the new column
CREATE INDEX IF NOT EXISTS idx_stg_asset_tasks_task_name_clean
ON data_staging.stg_asset_tasks(task_name_clean);

-- Update existing data using regex:
-- 1. Remove leading "N. " pattern (digits + period + space)
-- 2. Remove trailing " N" pattern (space + digits at end)
UPDATE data_staging.stg_asset_tasks
SET task_name_clean = TRIM(
    regexp_replace(
        regexp_replace(task_name, '^\d+\.\s*', ''),  -- Remove leading "N. "
        '\s+\d+$', ''                                  -- Remove trailing " N"
    )
)
WHERE task_name IS NOT NULL;

-- Comment for documentation
COMMENT ON COLUMN data_staging.stg_asset_tasks.task_name_clean IS 'Task name with sequence number prefix and revision suffix removed';
