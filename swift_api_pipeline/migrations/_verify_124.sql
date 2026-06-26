-- Verification for migration 124 (run via MCP execute_sql).
-- 1. Both objects exist.
SELECT to_regclass('data_staging.stg_calendar_events') AS t,
       to_regclass('agent.calendar_summary_parse')    AS c;

-- 2. CHECK constraints reject bad rows (each should ERROR).
-- INSERT INTO data_staging.stg_calendar_events (event_id, event_kind) VALUES ('t1','bogus');
-- INSERT INTO data_staging.stg_calendar_events (event_id, event_kind, leave_type_normalized) VALUES ('t2','holiday','VL');
-- DELETE FROM data_staging.stg_calendar_events WHERE event_id IN ('t1','t2');

-- 3. schema_metadata rows present.
SELECT schema_name, table_name FROM agent.schema_metadata
WHERE table_name IN ('stg_calendar_events','calendar_summary_parse');
