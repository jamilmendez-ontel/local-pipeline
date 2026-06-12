-- 093_report_targets_add_groups.sql
-- Add new Open Items Report groups: AT&T PA (base + Turf 6) and four VZW
-- groups (CGC Embedded, CGC Small Cell, Mountain Plains, CAR-TN).
--
-- reference.report_targets.report_group is a GENERATED column derived from
-- project_name (migration 058). To extend the CASE expression it must be
-- dropped and re-added, but two analytics views project rt.report_group, so
-- they have to be dropped first and recreated after, then re-granted.
-- All in one transaction so a failure rolls the whole thing back.
--
-- New mappings:
--   Ericsson/AT&T/PA%               -> 'AT&T PA'            (base + Turf 6)
--   VZW/CGC - Embedded%             -> 'VZW CGC EMBEDDED'
--   VZW/CGC - Small Cell%           -> 'VZW CGC SMALL CELL'
--   VZW/Mountain Plains - Embedded% -> 'VZW MP'
--   VZW/CAR-TN - Embedded%          -> 'VZW CAR/TN'
--
-- TMO PA Overlay (Ericsson/T-Mobile/PA - Overlay) is left untouched as its
-- own group. AT&T PA does NOT include any Overlay (no AT&T PA Overlay exists
-- in Swift).

BEGIN;

-- ── 1. Drop dependent views (punch_requirements references completed_sites) ──
DROP VIEW IF EXISTS analytics.v_open_items_punch_requirements;
DROP VIEW IF EXISTS analytics.v_open_items_completed_sites;

-- ── 2. Recreate the generated report_group column with the new branches ──
DROP INDEX IF EXISTS reference.idx_report_targets_report_group;
ALTER TABLE reference.report_targets DROP COLUMN IF EXISTS report_group;
ALTER TABLE reference.report_targets
ADD COLUMN report_group TEXT GENERATED ALWAYS AS (
  CASE
    WHEN project_name LIKE 'Ericsson/AT&T/KS%'                 THEN 'AT&T KS'
    WHEN project_name LIKE 'Ericsson/AT&T/New England%'        THEN 'AT&T NE'
    WHEN project_name LIKE 'Ericsson/AT&T/OH%'                 THEN 'AT&T OH'
    WHEN project_name LIKE 'Ericsson/AT&T/NTX%'                THEN 'AT&T NTX'
    WHEN project_name LIKE 'Ericsson/AT&T/STX%'                THEN 'AT&T STX'
    WHEN project_name LIKE 'Ericsson/AT&T/PA%'                 THEN 'AT&T PA'
    WHEN project_name LIKE 'Ericsson/T-Mobile/BAWA%'           THEN 'TMO BAWA'
    WHEN project_name LIKE 'Ericsson/T-Mobile/FL - Excalibur%'  THEN 'TMO FL Excalibur'
    WHEN project_name LIKE 'Ericsson/T-Mobile/GA - Overlay%'   THEN 'TMO GA Overlay'
    WHEN project_name LIKE 'Ericsson/T-Mobile/PA - Overlay%'   THEN 'TMO PA Overlay'
    WHEN project_name LIKE 'Ericsson/T-Mobile/SFL - Excalibur%' THEN 'TMO SFL Excalibur'
    WHEN project_name LIKE 'Ericsson/T-Mobile/UPNY - Overlay%' THEN 'TMO UPNY Overlay'
    WHEN project_name LIKE 'VZW/CGC - NSB Macro%'              THEN 'CGC NSB Macro'
    WHEN project_name LIKE 'VZW/CGC - Embedded%'               THEN 'VZW CGC EMBEDDED'
    WHEN project_name LIKE 'VZW/CGC - Small Cell%'             THEN 'VZW CGC SMALL CELL'
    WHEN project_name LIKE 'VZW/Mountain Plains - Embedded%'   THEN 'VZW MP'
    WHEN project_name LIKE 'VZW/CAR-TN - Embedded%'            THEN 'VZW CAR/TN'
    ELSE NULL
  END
) STORED;

CREATE INDEX IF NOT EXISTS idx_report_targets_report_group
    ON reference.report_targets (report_name, report_group)
    WHERE enabled;

COMMENT ON COLUMN reference.report_targets.report_group IS
    'Auto-derived workbook grouping key based on project_name pattern matching. Projects that share a report_group are combined into a single output file. NULL means the project_name pattern is unrecognized - add a new branch to the CASE expression via migration to support it.';

-- ── 3. Recreate the two views (verbatim from current defs) + re-grant ──
CREATE VIEW analytics.v_open_items_completed_sites AS
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
  AND tat.task_name ~* '^(\s*[0-9]+\.\s+)?final\s+cop(\s+[0-9]+)?\s*$';

COMMENT ON VIEW analytics.v_open_items_completed_sites IS
'BetaSites lookup for the Open Items Report: per-asset Final COP SUBMISSION date sourced from Swift asset_tasks. Filter: task_submitted_on IS NOT NULL (any submitted Final COP task qualifies, regardless of whether it has been approved yet). Date shown = task_submitted_on.';

CREATE VIEW analytics.v_open_items_punch_requirements AS
SELECT
  up.organization,
  up.project,
  rt.report_group,
  up.asset_id,
  up.asset_did,
  up.asset_name,
  regexp_replace(up.assigned_to, '(\s*\([^)]*\))+\s*$'::text, ''::text) AS task_assigned_to,
  up.status AS task_status,
  up.task_name,
  up.task_did,
  req.requirement_name,
  req.requirement_status,
  req.requirement_description,
  ('https://swiftprojects.io/#/app/assets/tasks/'::text || up.task_did) || '/requirements'::text AS swift_url,
  fc.final_cop_date
FROM data_staging.stg_targeted_task_requirements req
JOIN data_staging.stg_user_priorities up ON up.task_did = req.task_did
JOIN reference.report_targets rt
  ON rt.report_name = 'open_items_report'::text
 AND rt.enabled
 AND rt.org_did = up.org_did
 AND rt.project_did = up.project_did
LEFT JOIN LATERAL (
  SELECT v.final_cop_date
  FROM analytics.v_open_items_completed_sites v
  WHERE v.asset_did = up.asset_did
  ORDER BY v.final_cop_date DESC
  LIMIT 1
) fc ON true
WHERE req.report_name = 'open_items_report'::text
  AND up.task_name ~~* '%punch%'::text
  AND (up.status = ANY (ARRAY['pending'::text, 'in_progress'::text]))
  AND up.assigned_to IS NOT NULL;

-- Re-grant (DROP+CREATE loses grants). Matches pre-migration grants:
-- SELECT to anon/authenticated, ALL to service_role; postgres owns both.
GRANT SELECT ON analytics.v_open_items_completed_sites    TO anon, authenticated;
GRANT ALL    ON analytics.v_open_items_completed_sites    TO service_role;
GRANT SELECT ON analytics.v_open_items_punch_requirements TO anon, authenticated;
GRANT ALL    ON analytics.v_open_items_punch_requirements TO service_role;

-- ── 4. Presentation metadata for the 5 new groups ──
INSERT INTO reference.report_group_meta
  (report_name, report_group, title, file_label, cadence, logo_filename, sort_order, carrier)
VALUES
  ('open_items_report','AT&T PA',           'AT&T/PA',            'TSC AT&T PA',            'monday','AT&T logo.jpg',    15,'ATT'),
  ('open_items_report','VZW CGC EMBEDDED',  'VZW/CGC Embedded',   'TSC VZW CGC Embedded',   'friday','Verizon logo.png', 31,'VZW'),
  ('open_items_report','VZW CGC SMALL CELL','VZW/CGC Small Cell', 'TSC VZW CGC Small Cell', 'friday','Verizon logo.png', 32,'VZW'),
  ('open_items_report','VZW MP',            'VZW/Mountain Plains','TSC VZW Mountain Plains','friday','Verizon logo.png', 33,'VZW'),
  ('open_items_report','VZW CAR/TN',        'VZW/CAR-TN',         'TSC VZW CAR-TN',         'friday','Verizon logo.png', 34,'VZW');

-- ── 5. report_targets rows (report_group auto-computes from project_name) ──
INSERT INTO reference.report_targets (report_name, org_did, org_name, project_did, project_name)
SELECT v.report_name, v.org_did,
       (SELECT org_name FROM reference.report_targets
         WHERE org_did = '-LSLnbjcxlr7_LrWCzeH' AND org_name IS NOT NULL LIMIT 1),
       v.project_did, v.project_name
FROM (VALUES
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-NvX_hS1c923_isjFfQR','Ericsson/AT&T/PA'),
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-OArSzsR0VBTAjibJbE2','Ericsson/AT&T/PA (Turf 6)'),
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-Mb7iVV00Z5ZTy-W27jH','VZW/CGC - Embedded'),
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-MSdEk1C6R0fqWVWIkZY','VZW/CGC - Small Cell'),
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-OHETpPon_dQXuQ5fptO','VZW/Mountain Plains - Embedded'),
  ('open_items_report','-LSLnbjcxlr7_LrWCzeH','-NhmLZOwhwj0y-n5Ibvi','VZW/CAR-TN - Embedded')
) AS v(report_name, org_did, project_did, project_name);

COMMIT;
