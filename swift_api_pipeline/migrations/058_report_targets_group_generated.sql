-- migrations/058_report_targets_group_generated.sql
-- Make reference.report_targets.report_group a GENERATED column so new
-- projects are auto-categorized based on project_name pattern. Any
-- future variant like '(Turf 6) (2027)' or '(Turf 7)' lands in the
-- right group with zero manual intervention.

DROP INDEX IF EXISTS reference.idx_report_targets_report_group;

ALTER TABLE reference.report_targets DROP COLUMN IF EXISTS report_group;

ALTER TABLE reference.report_targets
ADD COLUMN report_group TEXT GENERATED ALWAYS AS (
  CASE
    WHEN project_name LIKE 'Ericsson/AT&T/KS%'                THEN 'AT&T KS'
    WHEN project_name LIKE 'Ericsson/AT&T/New England%'       THEN 'AT&T NE'
    WHEN project_name LIKE 'Ericsson/AT&T/OH%'                THEN 'AT&T OH'
    WHEN project_name LIKE 'Ericsson/AT&T/NTX%'               THEN 'AT&T NTX'
    WHEN project_name LIKE 'Ericsson/AT&T/STX%'               THEN 'AT&T STX'
    WHEN project_name LIKE 'Ericsson/T-Mobile/BAWA%'          THEN 'TMO BAWA'
    WHEN project_name LIKE 'Ericsson/T-Mobile/FL - Excalibur%' THEN 'TMO FL Excalibur'
    WHEN project_name LIKE 'Ericsson/T-Mobile/GA - Overlay%'  THEN 'TMO GA Overlay'
    WHEN project_name LIKE 'Ericsson/T-Mobile/PA - Overlay%'  THEN 'TMO PA Overlay'
    WHEN project_name LIKE 'Ericsson/T-Mobile/SFL - Excalibur%' THEN 'TMO SFL Excalibur'
    WHEN project_name LIKE 'Ericsson/T-Mobile/UPNY - Overlay%' THEN 'TMO UPNY Overlay'
    WHEN project_name LIKE 'VZW/CGC - NSB Macro%'             THEN 'CGC NSB Macro'
    ELSE NULL
  END
) STORED;

CREATE INDEX IF NOT EXISTS idx_report_targets_report_group
    ON reference.report_targets (report_name, report_group)
    WHERE enabled;

COMMENT ON COLUMN reference.report_targets.report_group IS
    'Auto-derived workbook grouping key based on project_name pattern matching. Projects that share a report_group are combined into a single output file. Yearly variants like (Turf 6) (2027) get categorized automatically. NULL means the project_name pattern is unrecognized - add a new branch to the CASE expression via migration to support it.';
