-- migrations/013_task_name_clean_all_tables.sql
-- Add task_name_clean / task_clean columns to all staging tables with task names

-- ============================================================
-- stg_user_priorities
-- ============================================================
ALTER TABLE data_staging.stg_user_priorities
ADD COLUMN IF NOT EXISTS task_name_clean TEXT;

UPDATE data_staging.stg_user_priorities
SET task_name_clean = TRIM(
    regexp_replace(
        regexp_replace(task_name, '^\d+\.\s*', ''),
        '\s+\d+$', ''
    )
)
WHERE task_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_task_name_clean
ON data_staging.stg_user_priorities(task_name_clean);

-- ============================================================
-- stg_qa_form
-- ============================================================
ALTER TABLE data_staging.stg_qa_form
ADD COLUMN IF NOT EXISTS task_clean TEXT;

UPDATE data_staging.stg_qa_form
SET task_clean = TRIM(
    regexp_replace(
        regexp_replace(task, '^\d+\.\s*', ''),
        '\s+\d+$', ''
    )
)
WHERE task IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stg_qa_form_task_clean
ON data_staging.stg_qa_form(task_clean);

-- ============================================================
-- stg_timer_activities
-- ============================================================
ALTER TABLE data_staging.stg_timer_activities
ADD COLUMN IF NOT EXISTS task_clean TEXT;

UPDATE data_staging.stg_timer_activities
SET task_clean = TRIM(
    regexp_replace(
        regexp_replace(task, '^\d+\.\s*', ''),
        '\s+\d+$', ''
    )
)
WHERE task IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stg_timer_activities_task_clean
ON data_staging.stg_timer_activities(task_clean);
