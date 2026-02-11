-- Migration 018: Staging Table Relationships
-- Drop FK constraints (pipeline does truncate+reload, FKs block that)
-- Add missing indexes on join columns
-- Update schema_metadata with relationship info
--
-- Relationship hierarchy:
--   stg_organizations
--     └── stg_projects              (org_did)
--           ├── stg_assets           (project_did)
--           │     ├── stg_asset_tasks   (asset_did)
--           │     ├── stg_qa_form       (site_id = asset_id)
--           │     ├── stg_timer_activities (site_id = asset_id)
--           │     └── stg_user_priorities  (asset_did)
--           ├── stg_timer_activities  (project_did)
--           └── stg_user_priorities   (project_did)
--   stg_ar_aging      (standalone)
--   stg_sales_detail  (standalone)

-- ============================================================
-- 1. Drop FK constraints that block pipeline truncate+reload
-- ============================================================
ALTER TABLE data_staging.stg_asset_tasks DROP CONSTRAINT IF EXISTS fk_stg_asset_tasks_project;
ALTER TABLE data_staging.stg_timer_activities DROP CONSTRAINT IF EXISTS fk_timer_project;

-- ============================================================
-- 2. Add missing indexes on join columns
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_site_id
    ON data_staging.stg_qa_form(site_id);

CREATE INDEX IF NOT EXISTS idx_stg_user_priorities_asset_did
    ON data_staging.stg_user_priorities(asset_did);

-- ============================================================
-- 3. Insert missing table-level metadata rows
-- ============================================================
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, related_tables)
VALUES
    ('data_staging', 'stg_assets', NULL,
     'Aggregated site/cell tower data with task status counts per asset',
     'Central hub table linking projects to tasks, QA forms, timer activities, and user priorities. Each row is a unique site within a project.',
     ARRAY['stg_projects (via project_did)', 'stg_asset_tasks (via asset_did)', 'stg_qa_form (via asset_id = site_id)', 'stg_timer_activities (via asset_id = site_id)', 'stg_user_priorities (via asset_did)']),

    ('data_staging', 'stg_user_priorities', NULL,
     'Task scheduling and approval workflow data from user priority queues',
     'Tracks task assignments, scheduling, submissions, approvals, rejections, and cancellations across organizations and projects.',
     ARRAY['stg_organizations (via org_did)', 'stg_projects (via project_did)', 'stg_assets (via asset_did)']),

    ('data_staging', 'stg_ar_aging', NULL,
     'Accounts receivable aging report from QuickBooks via daily email',
     'Standalone financial table. Daily snapshot of outstanding invoices with aging buckets, amounts, and open balances by customer.',
     NULL),

    ('data_staging', 'stg_sales_detail', NULL,
     'Sales detail report from QuickBooks via daily email',
     'Standalone financial table. Transaction-level sales data with quantities, prices, amounts, and PO numbers by customer.',
     NULL)
ON CONFLICT DO NOTHING;

-- ============================================================
-- 4. Update existing table-level metadata with relationships
-- ============================================================

-- stg_organizations: top of hierarchy
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_projects (via org_did)', 'stg_user_priorities (via org_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_organizations' AND column_name IS NULL;

-- stg_projects: links up to orgs, down to assets/timer/priorities
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_organizations (via org_did)', 'stg_assets (via project_did)', 'stg_asset_tasks (via project_did)', 'stg_timer_activities (via project_did)', 'stg_user_priorities (via project_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_projects' AND column_name IS NULL;

-- stg_asset_tasks: links to assets and projects
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via asset_did)', 'stg_projects (via project_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_asset_tasks' AND column_name IS NULL;

-- stg_qa_form: links to assets via site_id = asset_id
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_qa_form' AND column_name IS NULL;

-- stg_timer_activities: links to assets and projects
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)', 'stg_projects (via project_did)'],
    updated_at = now()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name IS NULL;
