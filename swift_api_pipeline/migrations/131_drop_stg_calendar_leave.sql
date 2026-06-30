-- migrations/131_drop_stg_calendar_leave.sql
-- Retire the old calendar-leave pipeline's frozen staging table. The pipeline
-- was restructured into data_staging.stg_calendar_events (migration 124/125);
-- analytics.v_calendar_leave + v_calendar_leave_daily were repointed onto the
-- conformed table in migration 129. stg_calendar_leave has been frozen (no
-- writes) since the 2026-06-26 cutover and has ZERO dependent objects
-- (verified via pg_depend), so the drop is safe. Also clears its stale
-- agent.schema_metadata rows so DARA/the agent layer stops seeing a dead table.
BEGIN;

DELETE FROM agent.schema_metadata
WHERE schema_name = 'data_staging' AND table_name = 'stg_calendar_leave';

DROP TABLE IF EXISTS data_staging.stg_calendar_leave;

COMMIT;
