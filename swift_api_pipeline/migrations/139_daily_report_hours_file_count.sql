-- 139_daily_report_hours_file_count.sql
-- Add per-requirement uploaded-file count to the daily-report hours staging table,
-- so the Ontel People Browse hover card can show a "has file(s) to review" indicator.
--
-- Source: metrics.fileUploadedCount from the Swift /api/asset-tasks/{did}/requirements
-- payload, which the daily-reports extractor already fetches and stores in
-- data_raw.raw_daily_reports (source_type='requirement'). No new Swift calls needed.
--
-- Grain: one row per (task_did, req_id). Report total = SUM(file_uploaded_count).
-- Attachments are effectively always optional (only 1 requirement in all history has
-- minimumFileCount > 0), so this is an informational count, not a required-file check.

ALTER TABLE data_staging.stg_daily_report_hours
    ADD COLUMN IF NOT EXISTS file_uploaded_count int NOT NULL DEFAULT 0;

-- One-time backfill from the raw payloads we already have (~1,543 rows expected > 0).
-- raw_daily_reports.data is double-encoded: a jsonb *string* holding the JSON text,
-- so unwrap with #>> '{}' before re-casting to jsonb.
UPDATE data_staging.stg_daily_report_hours s
SET file_uploaded_count = COALESCE(
        (((r.data #>> '{}')::jsonb) #>> '{metrics,fileUploadedCount}')::int, 0)
FROM data_raw.raw_daily_reports r
WHERE r.source_type = 'requirement'
  AND r.task_did = s.task_did
  AND r.source_id = s.req_id
  AND COALESCE(
        (((r.data #>> '{}')::jsonb) #>> '{metrics,fileUploadedCount}')::int, 0) <> s.file_uploaded_count;
