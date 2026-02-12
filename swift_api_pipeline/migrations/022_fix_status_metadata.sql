-- Migration 022: Fix incorrect status value metadata
-- requirement_status uses workflow statuses (not Pass/Fail)
-- project_status uses in_progress/complete/pending (not active/archived)

-- Fix v_qa_forms.requirement_status metadata
UPDATE agent.schema_metadata
SET description = 'QA requirement workflow status',
    business_context = 'Values: pending, submitted, approved, cancelled, in_progress. Use approved as "pass" and cancelled as "fail" for pass-rate calculations.',
    updated_at = NOW()
WHERE schema_name = 'analytics'
  AND table_name = 'v_qa_forms'
  AND column_name = 'requirement_status';

-- Fix stg_projects.status metadata
UPDATE agent.schema_metadata
SET description = 'Project lifecycle status',
    business_context = 'Values: in_progress, complete, pending. User may say: "active projects" (= in_progress), "finished projects" (= complete), "project status".',
    updated_at = NOW()
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_projects'
  AND column_name = 'status';

-- Add mv_project_summary.project_status metadata (was missing)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
('analytics', 'mv_project_summary', 'project_status',
 'Project lifecycle status',
 'Values: in_progress, complete, pending. Filter by in_progress for "active" projects. From stg_projects.status.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    updated_at = NOW();

-- Also add task_status metadata for v_asset_tasks (commonly queried)
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context)
VALUES
('analytics', 'v_asset_tasks', 'task_status',
 'Task workflow status',
 'Values: pending, in_progress, submitted, approved, rejected, cancelled. "approved" = completed/done. The majority being "pending" is normal (future scheduled work).')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    updated_at = NOW();
