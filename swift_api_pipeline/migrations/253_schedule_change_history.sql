-- =============================================================================
-- 253_schedule_change_history.sql
-- Schedule Change History: HR schedule-changes Google Sheet -> DRMC directory
-- timeline (/employees/[id] "Schedule history" section).
-- Spec: ai-projects/docs/superpowers/specs/2026-09-02-schedule-change-history.md
--
-- Why this shape:
--   * The sheet is hand-edited in place (no stable row ids), so data_raw is a
--     full-replace snapshot per run keyed (sheet_tab, row_index) and staging is
--     rebuilt from parsed rows each run. Loader: sync_schedule_changes.py.
--   * staging PK includes sheet_tab because the same (emp_id, start_date,
--     window) legitimately appears in both a current team tab and an archived
--     period tab.
--   * The view is named v_employee_schedule_history to sit beside
--     v_employee_directory (migration 155), which DRMC already reads.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_employee_schedule_history;
--   DROP TABLE IF EXISTS data_staging.stg_schedule_change_history;
--   DROP TABLE IF EXISTS data_raw.raw_schedule_changes;
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL AND (
--     (schema_name = 'data_raw'     AND table_name = 'raw_schedule_changes') OR
--     (schema_name = 'data_staging' AND table_name = 'stg_schedule_change_history') OR
--     (schema_name = 'analytics'    AND table_name = 'v_employee_schedule_history'));
--
-- APPLIED + VERIFIED 2026-09-02 ~08:05 ET against voqfjfngdpcvevbkikud via the
-- Supabase MCP (whole file, atomic BEGIN/COMMIT). Pre-flight: 0 name collisions,
-- 253 free, ref_employees has emp_id/email/effective_date/full_name/first_name/
-- nickname. Post-apply: both tables + view exist, view selects (0 rows), 3
-- schema_metadata rows present.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. Tables
-- =============================================================================

CREATE TABLE data_raw.raw_schedule_changes (
    sheet_tab     text NOT NULL,
    row_index     integer NOT NULL,           -- 0-based grid row within the tab
    payload       jsonb NOT NULL,             -- every cell as text, header-keyed
    row_hash      text NOT NULL,
    load_run_id   uuid NOT NULL,
    source_system text NOT NULL DEFAULT 'gsheet_schedule_changes',
    extracted_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sheet_tab, row_index)
);

COMMENT ON TABLE data_raw.raw_schedule_changes IS
    'Raw rows of the HR schedule-changes Google Sheet (1yX3D3ykzt8eZx6rMlCnuMf_Ei6hCt9BUh7AdBGi_U4Q), full-replace per sync run.';

CREATE TABLE data_staging.stg_schedule_change_history (
    emp_id           text NOT NULL,
    member_name      text NOT NULL,
    role             text,
    sheet_tab        text NOT NULL,
    shift_start_pht  text,
    shift_end_pht    text,
    shift_start_et   text,
    shift_end_et     text,
    shift_code       text,                    -- DS / NS as written
    work_arrangement text,                    -- 5DWW / 4DWW as written
    reg_hours        integer,
    rest_day         text,
    rdo_to           date,
    rdo_day          text,
    start_date       date NOT NULL,
    end_date         date,                    -- NULL = open-ended
    change_kind      text NOT NULL CHECK (change_kind IN ('one_day','temporary','ongoing')),
    notes            text,
    row_hash         text NOT NULL,
    load_run_id      uuid NOT NULL,
    source_system    text NOT NULL DEFAULT 'gsheet_schedule_changes',
    extracted_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (emp_id, sheet_tab, start_date, shift_start_pht)
);

COMMENT ON TABLE data_staging.stg_schedule_change_history IS
    'Parsed schedule-change periods per member from the HR schedule sheet; rebuilt from data_raw.raw_schedule_changes each sync run.';

-- =============================================================================
-- 2. Security baseline (DATABASE_ARCHITECTURE.md section 5)
-- =============================================================================

ALTER TABLE data_raw.raw_schedule_changes            ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_schedule_change_history ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON data_raw.raw_schedule_changes            FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_schedule_change_history FROM anon, authenticated;
GRANT ALL ON data_raw.raw_schedule_changes            TO service_role;
GRANT ALL ON data_staging.stg_schedule_change_history TO service_role;

-- =============================================================================
-- 3. Serving view
-- =============================================================================

CREATE VIEW analytics.v_employee_schedule_history AS
WITH latest_emp AS (
    SELECT DISTINCT ON (emp_id) emp_id, email
    FROM reference.ref_employees
    ORDER BY emp_id, effective_date DESC NULLS LAST
),
h AS (
    SELECT s.*,
           (s.start_date <= (now() AT TIME ZONE 'Asia/Manila')::date
            AND (s.end_date IS NULL
                 OR s.end_date >= (now() AT TIME ZONE 'Asia/Manila')::date)) AS covers_today
    FROM data_staging.stg_schedule_change_history s
)
SELECT h.emp_id, e.email, h.member_name, h.role, h.sheet_tab,
       h.shift_start_pht, h.shift_end_pht, h.shift_start_et, h.shift_end_et,
       h.shift_code, h.work_arrangement, h.reg_hours,
       h.rest_day, h.rdo_to, h.rdo_day,
       h.start_date, h.end_date, h.change_kind, h.notes,
       (h.covers_today AND ROW_NUMBER() OVER (
            PARTITION BY h.emp_id
            ORDER BY h.covers_today DESC, h.start_date DESC,
                     h.end_date ASC NULLS LAST, h.shift_start_pht
        ) = 1) AS is_current
FROM h
LEFT JOIN latest_emp e USING (emp_id);

COMMENT ON VIEW analytics.v_employee_schedule_history IS
    'Per-member schedule-change history for the DRMC directory member page; is_current = latest-starting row covering today (PHT).';

REVOKE ALL ON analytics.v_employee_schedule_history FROM anon, authenticated;
GRANT SELECT ON analytics.v_employee_schedule_history TO service_role;

-- =============================================================================
-- 4. agent.schema_metadata
-- =============================================================================

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT v.schema_name, v.table_name, NULL, v.description, v.business_context, v.data_notes, v.related_tables
FROM (VALUES
    ('data_raw', 'raw_schedule_changes',
     'Raw rows of the HR schedule-changes Google Sheet, one row per (tab, grid row).',
     'Landing snapshot for the DRMC directory Schedule history timeline. The sheet is HR''s editing surface (per-team tabs sharing one template; Summary tab skipped as a mirror).',
     'Full-replace per sync run (sheet rows are edited in place; deletions must disappear). payload = header-keyed cell text. Loader: sync_schedule_changes.py.',
     ARRAY['data_staging.stg_schedule_change_history (rebuilt from this)']),
    ('data_staging', 'stg_schedule_change_history',
     'Parsed schedule-change periods: one row per member per approved schedule period.',
     'Grain: (emp_id, sheet_tab, start_date, shift_start_pht). change_kind one_day/temporary/ongoing; end_date NULL = open-ended. Notes kept verbatim (approvers, reasons, weekday exceptions).',
     'Rebuilt from raw each run. emp_id is the sheet ID Number (same keyspace as reference.ref_employees.emp_id); blank sheet ids resolved by unique first+last name match, unresolved rows stay raw-only. PII: employee names.',
     ARRAY['data_raw.raw_schedule_changes', 'reference.ref_employees (emp_id)', 'analytics.v_employee_schedule_history']),
    ('analytics', 'v_employee_schedule_history',
     'Serving view for the DRMC directory member page Schedule history section.',
     'Adds email (latest ref_employees row per emp_id) and is_current = the member''s latest-starting row covering today in PHT. Read by ontel-people via PostgREST as service_role.',
     'Order client-side by start_date DESC. Sibling of v_employee_directory (migration 155).',
     ARRAY['data_staging.stg_schedule_change_history', 'reference.ref_employees'])
) AS v(schema_name, table_name, description, business_context, data_notes, related_tables)
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = v.schema_name AND m.table_name = v.table_name AND m.column_name IS NULL
);

COMMIT;
