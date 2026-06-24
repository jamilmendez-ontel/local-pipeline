-- 120: Capture the Swift "assignedTo" approver group on daily reports.
-- Used by analytics.v_daily_report_approvals for the leads' approval cockpit.
ALTER TABLE data_staging.stg_daily_reports
  ADD COLUMN IF NOT EXISTS assigned_approver text;

COMMENT ON COLUMN data_staging.stg_daily_reports.assigned_approver IS
  'Swift assignedTo group name (e.g. "Daily Report Approvers - CG3"). Approver QUEUE, not an individual. NULL on pending/unsubmitted shells.';
