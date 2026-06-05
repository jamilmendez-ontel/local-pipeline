-- 062_open_items_carrier_column.sql
-- Adds carrier column to reference.report_group_meta. Drives the
-- per-carrier zip bundling in the Open Items Report email (Phase E1).
-- Values: 'ATT', 'TMO', 'VZW' -- derived from the existing logo_filename.

BEGIN;

ALTER TABLE reference.report_group_meta
  ADD COLUMN IF NOT EXISTS carrier TEXT;

UPDATE reference.report_group_meta SET carrier = 'ATT'
  WHERE carrier IS NULL
    AND (logo_filename ILIKE '%AT&T%' OR logo_filename ILIKE '%att%');

UPDATE reference.report_group_meta SET carrier = 'TMO'
  WHERE carrier IS NULL
    AND (logo_filename ILIKE '%T-Mobile%' OR logo_filename ILIKE '%tmo%');

UPDATE reference.report_group_meta SET carrier = 'VZW'
  WHERE carrier IS NULL
    AND (logo_filename ILIKE '%Verizon%' OR logo_filename ILIKE '%vzw%');

-- Fail loudly if any row didn't classify so we don't silently ship a bad
-- carrier mapping. If this trips, the manual fix is one UPDATE per row.
DO $$
DECLARE missing INT;
BEGIN
  SELECT COUNT(*) INTO missing
  FROM reference.report_group_meta
  WHERE carrier IS NULL;
  IF missing > 0 THEN
    RAISE EXCEPTION 'carrier still NULL for % rows in reference.report_group_meta', missing;
  END IF;
END $$;

ALTER TABLE reference.report_group_meta
  ALTER COLUMN carrier SET NOT NULL;

ALTER TABLE reference.report_group_meta
  ADD CONSTRAINT report_group_meta_carrier_check
  CHECK (carrier IN ('ATT', 'TMO', 'VZW'));

COMMIT;
