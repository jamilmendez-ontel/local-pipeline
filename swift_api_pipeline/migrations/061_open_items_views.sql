-- migrations/061_open_items_views.sql
-- Analytics views feeding the Open Items Report.
--
-- The underlying staging tables (stg_targeted_asset_tasks /
-- stg_targeted_task_requirements / reference.report_targets) stay
-- generic so future reports can share them. These views bake in the
-- `report_name = 'open_items_report'` filter + the open-items punch
-- categorisation + the BetaSites enrichment, giving the report code a
-- clean read interface:
--
--   v_open_items_punch_requirements  — one row per pending punch
--                                       requirement, scoped to the OIR
--                                       projects, with final_cop_date
--                                       enriched from BetaSites lookup.
--   v_open_items_completed_sites     — assets whose Final COP task is
--                                       approved in Swift (the
--                                       BetaSites concept) with the
--                                       approved date as
--                                       final_cop_date.

BEGIN;

-- ── BetaSites lookup ──────────────────────────────────────────────
CREATE OR REPLACE VIEW analytics.v_open_items_completed_sites AS
SELECT
  rt.report_group,
  rt.org_did,
  rt.project_did,
  rt.project_name,
  tat.asset_id           AS asset_did,        -- canonical Swift asset DID
  tat.asset_identifier   AS asset_id,         -- human-readable asset_id
  tat.asset_name,
  tat.task_name,
  tat.task_approved_on   AS final_cop_date
FROM data_staging.stg_targeted_asset_tasks tat
JOIN reference.report_targets rt
  ON rt.report_name = 'open_items_report'
 AND rt.enabled
 AND rt.project_did = tat.project_did
WHERE tat.task_status = 'approved'
  AND tat.task_approved_on IS NOT NULL
  -- Final COP task variants we accept: "1. Final COP", "2. Final COP",
  -- "3. Final COP", bare "Final COP", "Final COP 2", etc. Excludes any
  -- task with extra words like "Complete", "Notified", "Invoiced".
  AND tat.task_name ~* '^(\s*[0-9]+\.\s+)?final\s+cop(\s+[0-9]+)?\s*$';

COMMENT ON VIEW analytics.v_open_items_completed_sites IS
'BetaSites lookup for the Open Items Report: per-asset Final COP approval date sourced from Swift asset_tasks. Used to enrich each punch row with the asset''s Final COP date if completed.';

-- ── Punch requirements (the report''s working dataset) ────────────
CREATE OR REPLACE VIEW analytics.v_open_items_punch_requirements AS
SELECT
  up.organization,
  up.project,
  rt.report_group,
  up.asset_id,
  up.asset_did,
  up.asset_name,
  -- Strip trailing parenthetical groups (e.g. " (CGC)", " (AT&T)",
  -- chained ones like " (KS) (AT&T)") from assigned_to. Source data
  -- in stg_user_priorities is left untouched.
  REGEXP_REPLACE(up.assigned_to, '(\s*\([^)]*\))+\s*$', '') AS task_assigned_to,
  up.status              AS task_status,
  up.task_name,
  up.task_did,
  req.requirement_name,
  req.requirement_status,
  req.requirement_description,
  'https://swiftprojects.io/#/app/assets/tasks/' || up.task_did || '/requirements' AS swift_url,
  fc.final_cop_date
FROM data_staging.stg_targeted_task_requirements req
JOIN data_staging.stg_user_priorities up
  ON up.task_did = req.task_did
JOIN reference.report_targets rt
  ON rt.report_name = 'open_items_report'
 AND rt.enabled
 AND rt.org_did = up.org_did
 AND rt.project_did = up.project_did
LEFT JOIN LATERAL (
  -- BetaSites enrichment: pick the asset's most recent approved
  -- Final COP date (if any). When present, the report displays it on
  -- the detail page band; when null, the band shows "- -".
  SELECT v.final_cop_date
  FROM analytics.v_open_items_completed_sites v
  WHERE v.asset_did = up.asset_did
  ORDER BY v.final_cop_date DESC
  LIMIT 1
) fc ON TRUE
WHERE req.report_name = 'open_items_report'
  AND up.task_name ILIKE '%punch%'
  AND up.status IN ('pending', 'in_progress')
  AND up.assigned_to IS NOT NULL;

COMMENT ON VIEW analytics.v_open_items_punch_requirements IS
'Working dataset for the Open Items Report: one row per pending punch requirement for sites in scope. Joins targeted_task_requirements + user_priorities + report_targets + LEFT JOIN to completed_sites for the Final COP date. Used directly by report-automation/open-items-report.';

COMMIT;
