-- migrations/057_report_targets_add_group.sql
-- (Superseded by 058, which converts this column to a GENERATED column.
-- Kept as a placeholder to preserve migration numbering.)
--
-- Original intent: add a static `report_group` TEXT column to
-- reference.report_targets so Open Items Report knows which Swift
-- projects to combine into a single workbook (e.g. KS + KS Turf 6).
-- The static column was seeded via UPDATE, but a static column does
-- not auto-categorize new project rows — so 058 replaces it with a
-- generated column. This file is here for migration-number continuity.

-- No-op: 058 handles the column creation in its current form.
SELECT 1;
