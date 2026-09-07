-- =============================================================================
-- 255_schedule_roster_reconciliation.sql
-- Schedule Change History v2: declared schedule (HR schedule-changes sheet)
-- vs the roster (reference.ref_employees), surfaced as one serving view.
-- Spec: ai-projects/docs/superpowers/specs/2026-09-07-schedule-roster-reconciliation-design.md
--
-- Why this shape:
--   * DRMC's Late / undertime math reads the ROSTER shift (ref_employees
--     .shift_time_in_pht, migration 162), not the schedule sheet. When HR logs
--     a change in the sheet and the roster is not updated, every Late badge
--     for that member is measured against the wrong shift. This view names
--     those members so HR can fix the roster sheet (the sync then heals it).
--   * One row per CURRENT declared schedule (v_employee_schedule_history
--     .is_current), joined to the roster row (one row per emp_id). Times are
--     normalized to `time` so "9 PM" (sheet) and "9:00 PM" (roster) compare
--     equal; anything unparseable stays NULL and never flags.
--   * The Swift task-schedule comparison named in the 2026-09-02 spec is NOT
--     built: timed Swift schedules carry vendor/team assignees, zero members
--     (measured 2026-09-07 over 60 days), so there is nothing to reconcile.
--   * Read-only. The sheet and the roster stay HR's editing surfaces.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_schedule_roster_reconciliation;
--   DROP FUNCTION IF EXISTS analytics.shift_clock_pht(text);
--   DELETE FROM agent.schema_metadata WHERE column_name IS NULL
--     AND schema_name = 'analytics' AND table_name = 'v_schedule_roster_reconciliation';
--
-- APPLIED + VERIFIED 2026-09-07 ~02:55 ET against voqfjfngdpcvevbkikud via the
-- Supabase MCP (whole body, atomic BEGIN/COMMIT). Pre-flight: 0 name collisions,
-- 255 free, ref_employees = 122 rows / 122 emp_ids (one row per member), the
-- SELECT reproduced on live data first. Post-apply: 92 rows, 13 needs_review
-- (11 has_mismatch on active members + 2 not_in_roster), 1 schema_metadata row,
-- EXPLAIN ANALYZE 24 ms / 114 shared buffers for the needs_review filter.
-- Pre-merge review fixes RE-APPLIED live ~03:30 ET (CREATE OR REPLACE + grants only):
-- (1) REVOKE was FROM anon, authenticated, which leaves the default PUBLIC EXECUTE in
--     place; now FROM PUBLIC + GRANT service_role (proacl = postgres, service_role).
-- (2) hour bounded to 1-12 and minutes to :00-:59 so '13 PM' / '9:60 PM' -> NULL
--     instead of an out-of-range cast error; no-space '9PM' / '9:30PM' now parse.
--     Verified: 13 PM -> NULL, 0 AM -> NULL, 9 PM -> 21:00, 9PM -> 21:00, 9:30PM ->
--     21:30, 12:30 am -> 00:30, '' -> NULL; view still 13 needs_review.
-- =============================================================================

BEGIN;

-- "9 PM" / "9:00 PM" / "9PM" / " 10:30 am " -> time; anything else -> NULL (never flags).
CREATE OR REPLACE FUNCTION analytics.shift_clock_pht(raw text)
RETURNS time
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    -- Guard: 12-hour clock only (1-12, optional :MM), so an out-of-range typo like
    -- "13 PM" in the hand-typed sheet falls through to NULL instead of failing the
    -- cast (which would take down the whole view, not one row).
    -- Normalize: "9 PM" / "9PM" -> "9:00 PM"; "9:30PM" -> "9:30 PM"; then cast
    -- ('9 PM'::time and '9PM'::time both fail without the :00).
    SELECT CASE
        WHEN raw ~* '^\s*(0?[1-9]|1[0-2])(:[0-5]\d)?\s*(AM|PM)\s*$'
        THEN regexp_replace(
                 regexp_replace(trim(raw), '^(\d{1,2})\s*(AM|PM)$', '\1:00 \2', 'i'),
                 '^(\d{1,2}:\d{2})\s*(AM|PM)$', '\1 \2', 'i')::time
        ELSE NULL
    END;
$$;

COMMENT ON FUNCTION analytics.shift_clock_pht(text) IS
    'Parses a roster/sheet shift clock text ("9 PM", "9:00 PM") to time; NULL when unparseable.';

CREATE VIEW analytics.v_schedule_roster_reconciliation AS
WITH declared AS (
    SELECT h.emp_id, h.email, h.member_name, h.sheet_tab, h.change_kind,
           h.start_date, h.end_date,
           h.shift_start_pht, h.shift_end_pht, h.work_arrangement, h.rest_day
    FROM analytics.v_employee_schedule_history h
    WHERE h.is_current
),
joined AS (
    SELECT d.*,
           analytics.shift_clock_pht(d.shift_start_pht) AS declared_in,
           analytics.shift_clock_pht(d.shift_end_pht)   AS declared_out,
           CASE WHEN upper(trim(d.work_arrangement)) IN ('4DWW','5DWW')
                THEN upper(trim(d.work_arrangement)) END AS declared_arrangement,
           analytics.shift_clock_pht(r.shift_time_in_pht)  AS roster_in,
           analytics.shift_clock_pht(r.shift_time_out_pht) AS roster_out,
           CASE WHEN upper(trim(r.work_schedule)) IN ('4DWW','5DWW')
                THEN upper(trim(r.work_schedule)) END AS roster_arrangement,
           r.shift_time_in_pht  AS roster_in_pht,
           r.shift_time_out_pht AS roster_out_pht,
           (r.emp_id IS NOT NULL) AS in_roster,
           r.is_active AS roster_is_active
    FROM declared d
    LEFT JOIN reference.ref_employees r ON r.emp_id = d.emp_id
),
flagged AS (
    SELECT j.*,
           (j.declared_in  IS NOT NULL AND j.roster_in  IS NOT NULL AND j.declared_in  <> j.roster_in)  AS in_mismatch,
           (j.declared_out IS NOT NULL AND j.roster_out IS NOT NULL AND j.declared_out <> j.roster_out) AS out_mismatch,
           (j.declared_arrangement IS NOT NULL AND j.roster_arrangement IS NOT NULL
            AND j.declared_arrangement <> j.roster_arrangement) AS arrangement_mismatch
    FROM joined j
)
SELECT f.emp_id, f.email, f.member_name, f.sheet_tab, f.change_kind,
       f.start_date, f.end_date,
       f.shift_start_pht     AS declared_in_pht,
       f.shift_end_pht       AS declared_out_pht,
       f.declared_arrangement,
       f.rest_day            AS declared_rest_day,
       f.roster_in_pht, f.roster_out_pht, f.roster_arrangement,
       f.in_roster, f.roster_is_active,
       f.in_mismatch, f.out_mismatch, f.arrangement_mismatch,
       (f.in_mismatch OR f.out_mismatch OR f.arrangement_mismatch) AS has_mismatch,
       (NOT f.in_roster
        OR ((f.in_mismatch OR f.out_mismatch OR f.arrangement_mismatch)
            AND COALESCE(f.roster_is_active, false))) AS needs_review
FROM flagged f;

COMMENT ON VIEW analytics.v_schedule_roster_reconciliation IS
    'Current declared schedule (HR sheet) vs roster (ref_employees) per member; needs_review = missing from roster, or an active member whose shift/arrangement differs. Roster drives DRMC Late/undertime.';

REVOKE ALL ON analytics.v_schedule_roster_reconciliation FROM anon, authenticated;
GRANT SELECT ON analytics.v_schedule_roster_reconciliation TO service_role;
REVOKE ALL ON FUNCTION analytics.shift_clock_pht(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.shift_clock_pht(text) TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
SELECT 'analytics', 'v_schedule_roster_reconciliation', NULL,
       'Current declared schedule (HR schedule-changes sheet) side by side with the roster shift (reference.ref_employees), with mismatch flags.',
       'The roster shift drives DRMC Late and undertime (migration 162). needs_review rows are members HR should fix in the roster sheet: not in the roster, or an active member whose declared shift start/end or 4DWW/5DWW differs. Read by ontel-people (member page pill + /hr/schedule-mismatches) as service_role.',
       'One row per is_current row of v_employee_schedule_history. Clock texts normalized via analytics.shift_clock_pht ("9 PM" = "9:00 PM"); unparseable values stay NULL and never flag. Rest day is carried but not compared (the roster has no rest-day column). Not a Swift comparison: timed Swift schedules have vendor assignees only.',
       ARRAY['analytics.v_employee_schedule_history', 'reference.ref_employees', 'data_staging.stg_schedule_change_history']
WHERE NOT EXISTS (
    SELECT 1 FROM agent.schema_metadata m
    WHERE m.schema_name = 'analytics' AND m.table_name = 'v_schedule_roster_reconciliation' AND m.column_name IS NULL
);

COMMIT;
