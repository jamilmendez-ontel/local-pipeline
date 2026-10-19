-- 282_oir_car_tn_monday.sql
-- Open Items Report: move VZW/CAR-TN from the Friday run to the Monday run.
--
-- Requested by Jamil 2026-10-19: CAR-TN is no longer sent on Fridays with the
-- other Verizon groups; it goes out every Monday to its own Google Chat space.
-- The chat routing is in report-automation (chat_sender._SPLIT_SPACES: monday
-- VZW -> OPEN_ITEMS_CHAT_SPACE_MONDAY_VZW). Apply only AFTER that code is on
-- report-automation main, or main's Monday run posts CAR-TN into the AT&T/TMO
-- space.
--
-- Rollback: UPDATE ... SET cadence = 'friday' for the same row.

BEGIN;

UPDATE reference.report_group_meta
   SET cadence = 'monday',
       updated_at = now()
 WHERE report_name = 'open_items_report'
   AND report_group = 'VZW CAR/TN'
   AND cadence = 'friday';

DO $$
BEGIN
  IF (SELECT cadence FROM reference.report_group_meta
       WHERE report_name = 'open_items_report'
         AND report_group = 'VZW CAR/TN') IS DISTINCT FROM 'monday' THEN
    RAISE EXCEPTION 'VZW CAR/TN cadence is not monday after update';
  END IF;
END $$;

COMMIT;
