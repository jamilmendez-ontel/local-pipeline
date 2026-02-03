-- migrations/002_staging_views.sql
-- Staging views that flatten JSONB into columns

-- Staging view for organizations
CREATE OR REPLACE VIEW stg_organizations AS
SELECT
    id,
    run_id,
    loaded_at,
    -- Core fields
    data->>'id' AS org_id,
    data->>'name' AS org_name,
    (data->>'avc')::INTEGER AS avc,
    -- POC (Point of Contact) fields
    data->'poc'->>'id' AS poc_id,
    data->'poc'->>'name' AS poc_name,
    data->'poc'->>'email' AS poc_email,
    data->'poc'->>'firstName' AS poc_first_name,
    data->'poc'->>'lastName' AS poc_last_name,
    -- Metadata
    data->'createdBy'->>'id' AS created_by_id,
    to_timestamp((data->>'dateCreated')::bigint / 1000) AS date_created,
    to_timestamp((data->>'lastUpdated')::bigint / 1000) AS last_updated,
    -- Raw data for additional queries
    data AS raw_data
FROM raw_organizations;

-- Staging view for projects
CREATE OR REPLACE VIEW stg_projects AS
SELECT
    id,
    run_id,
    loaded_at,
    -- Core fields
    data->>'id' AS project_id,
    data->>'name' AS project_name,
    data->>'status' AS status,
    data->>'_org_id' AS org_id,
    data->>'_org_name' AS org_name,
    (data->>'isPrivate')::BOOLEAN AS is_private,
    data->>'locationOrientation' AS location_orientation,
    -- Metrics - Asset level
    (data->'metrics'->'asset'->>'taskCount')::INTEGER AS asset_task_count,
    (data->'metrics'->'asset'->>'taskPending')::INTEGER AS asset_task_pending,
    (data->'metrics'->'asset'->>'taskApproved')::INTEGER AS asset_task_approved,
    (data->'metrics'->'asset'->>'taskRejected')::INTEGER AS asset_task_rejected,
    (data->'metrics'->'asset'->>'taskCancelled')::INTEGER AS asset_task_cancelled,
    (data->'metrics'->'asset'->>'taskSubmitted')::INTEGER AS asset_task_submitted,
    (data->'metrics'->'asset'->>'taskInProgress')::INTEGER AS asset_task_in_progress,
    (data->'metrics'->'asset'->>'assetProjectCount')::INTEGER AS asset_project_count,
    (data->'metrics'->'asset'->>'milestoneCount')::INTEGER AS asset_milestone_count,
    -- Metrics - Project level
    (data->'metrics'->'project'->>'taskCount')::INTEGER AS project_task_count,
    (data->'metrics'->'project'->>'taskPending')::INTEGER AS project_task_pending,
    (data->'metrics'->'project'->>'taskApproved')::INTEGER AS project_task_approved,
    (data->'metrics'->'project'->>'taskRejected')::INTEGER AS project_task_rejected,
    (data->'metrics'->'project'->>'taskCancelled')::INTEGER AS project_task_cancelled,
    (data->'metrics'->'project'->>'taskSubmitted')::INTEGER AS project_task_submitted,
    (data->'metrics'->'project'->>'taskInProgress')::INTEGER AS project_task_in_progress,
    (data->'metrics'->'project'->>'milestoneCount')::INTEGER AS project_milestone_count,
    -- Metadata
    data->'createdBy'->>'id' AS created_by_id,
    to_timestamp((data->>'dateCreated')::bigint / 1000) AS date_created,
    to_timestamp((data->>'lastUpdated')::bigint / 1000) AS last_updated,
    to_timestamp((data->'metrics'->>'lastUpdated')::bigint / 1000) AS metrics_last_updated,
    -- Raw data for additional queries
    data AS raw_data
FROM raw_projects;
