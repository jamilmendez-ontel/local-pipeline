-- Migration 027: Add carrier_group to analytics.v_asset_tasks
-- Enables agent queries like "completion by carrier group" or "which carriers are behind"

CREATE OR REPLACE VIEW analytics.v_asset_tasks AS
SELECT
    at.task_did,
    at.task_name_clean,
    at.task_status,
    at.task_scheduled,
    at.task_approved_on,
    at.task_submitted_on,
    at.task_cancelled_on,
    at.task_assigned_to_name,
    at.task_assigned_to_email,
    at.task_submitted_by_name,
    at.task_approved_by_name,
    at.task_cancelled_by_name,
    at.asset_did,
    a.asset_id,
    a.asset_name,
    at.project_did,
    p.project_name,
    o.org_name,
    a.carrier_group
FROM data_staging.stg_asset_tasks at
JOIN data_staging.stg_assets a
    ON at.asset_did = a.asset_did AND at.project_did = a.project_did
JOIN data_staging.stg_projects p
    ON at.project_did = p.project_did
JOIN data_staging.stg_organizations o
    ON p.org_did = o.org_did;
