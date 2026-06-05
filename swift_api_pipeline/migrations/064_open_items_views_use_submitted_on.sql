-- 064_open_items_views_use_submitted_on.sql
-- Switch the Open Items Report's Final COP date from approvedOn to
-- submittedOn — full replacement, not COALESCE fallback.
--
-- Why: stakeholders want sites where the field tech has SUBMITTED the
-- Final COP to count as "done", not just sites where the manager has
-- already approved it. The displayed date is when the tech finished
-- the work, which is closer to operational reality.
--
-- Filter change: was (task_status='approved' AND task_approved_on IS
-- NOT NULL); now (task_submitted_on IS NOT NULL). Status is irrelevant
-- once the tech has submitted — submitted, approved, and re-opened
-- variants all show up as long as there's a submission timestamp.

BEGIN;

CREATE OR REPLACE VIEW analytics.v_open_items_completed_sites AS
SELECT
  rt.report_group,
  rt.org_did,
  rt.project_did,
  rt.project_name,
  tat.asset_id           AS asset_did,
  tat.asset_identifier   AS asset_id,
  tat.asset_name,
  tat.task_name,
  tat.task_submitted_on  AS final_cop_date
FROM data_staging.stg_targeted_asset_tasks tat
JOIN reference.report_targets rt
  ON rt.report_name = 'open_items_report'
 AND rt.enabled
 AND rt.project_did = tat.project_did
WHERE tat.task_submitted_on IS NOT NULL
  -- Final COP task variants we accept: "1. Final COP", "2. Final COP",
  -- "3. Final COP", bare "Final COP", "Final COP 2", etc. Excludes any
  -- task with extra words like "Complete", "Notified", "Invoiced".
  AND tat.task_name ~* '^(\s*[0-9]+\.\s+)?final\s+cop(\s+[0-9]+)?\s*$';

COMMENT ON VIEW analytics.v_open_items_completed_sites IS
'BetaSites lookup for the Open Items Report: per-asset Final COP SUBMISSION date sourced from Swift asset_tasks. Filter: task_submitted_on IS NOT NULL (any submitted Final COP task qualifies, regardless of whether it has been approved yet). Date shown = task_submitted_on.';

COMMIT;
