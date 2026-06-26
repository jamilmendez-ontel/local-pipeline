-- migrations/125_calendar_events_cutover.sql
-- GATED: apply only after the old-vs-new diff (Task 12) is clean.
-- Renames raw to the generic name and repoints the HR view contract.
-- Wrapped in a transaction so the whole migration is atomic/reversible.

BEGIN;

ALTER TABLE data_raw.raw_calendar_leave RENAME TO raw_calendar_events;

-- Drop views in dependency order (daily depends on leave) before recreating
-- with the new column names; CREATE OR REPLACE cannot rename/reorder columns.
DROP VIEW IF EXISTS analytics.v_calendar_leave_daily;
DROP VIEW IF EXISTS analytics.v_calendar_leave;

-- Preserve the HR app's read contract: same view name, new conformed base,
-- leave-only, excluding soft-deleted rows.
CREATE VIEW analytics.v_calendar_leave AS
SELECT event_id, summary_raw AS summary, leave_type, leave_type_normalized,
       team, team_normalized, person, person_note, rest_day_of_week,
       start_date, end_date, days, is_all_day, creator_email,
       event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'leave' AND NOT is_deleted;

-- Daily-exploded view, same scope.
CREATE VIEW analytics.v_calendar_leave_daily AS
SELECT e.*, gs::date AS leave_date
FROM analytics.v_calendar_leave e
CROSS JOIN LATERAL generate_series(e.start_date, e.end_date, interval '1 day') gs;

UPDATE agent.schema_metadata
SET table_name = 'raw_calendar_events',
    description = description || ' [renamed from raw_calendar_leave 2026-06-25]'
WHERE schema_name = 'data_raw' AND table_name = 'raw_calendar_leave';

COMMIT;
