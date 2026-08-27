-- 245_ref_holidays_amendments.sql
-- ref_holidays: one row per date (key fix), amendment tracking, history, watcher run log.
--
-- Why: 244 put holiday_type in the primary key, so a proclamation that CHANGES a day's type
-- (special -> regular, which Malacanang does: Nov 2 dropped 2025 / re-added 2026, Dec 24
-- flips, Eid dates moved) could be inserted as a second row instead of failing. A date has
-- exactly one type per calendar, so the key is (calendar, holiday_date) and a change is an
-- UPDATE. Every update/delete is copied to ref_holidays_history by trigger, and the row
-- itself keeps previous_type + amended_by so the amendment is visible in place.
--
-- pipeline.holiday_watch_runs backs swift_api_pipeline/holiday_feed_watcher.py: one row per
-- run, with the Official Gazette watermark (highest proclamation number already reviewed) and
-- the findings that were emailed. The watcher never writes ref_holidays; it proposes SQL.
--
-- Rollback:
--   DROP TABLE IF EXISTS pipeline.holiday_watch_runs;
--   DROP TRIGGER IF EXISTS trg_ref_holidays_history ON reference.ref_holidays;
--   DROP FUNCTION IF EXISTS reference.fn_ref_holidays_history();
--   DROP TABLE IF EXISTS reference.ref_holidays_history;
--   ALTER TABLE reference.ref_holidays DROP COLUMN updated_at, DROP COLUMN amended_by, DROP COLUMN previous_type;
--   ALTER TABLE reference.ref_holidays DROP CONSTRAINT ref_holidays_pkey,
--     ADD PRIMARY KEY (calendar, holiday_date, holiday_type);
--   DELETE FROM agent.schema_metadata WHERE table_name IN ('ref_holidays_history','holiday_watch_runs');

BEGIN;

-- 1. Key: one row per (calendar, date). Verified 2026-08-27: no duplicates on the 97 seed rows.
ALTER TABLE reference.ref_holidays DROP CONSTRAINT ref_holidays_pkey;
ALTER TABLE reference.ref_holidays ADD PRIMARY KEY (calendar, holiday_date);

-- 2. Amendment columns
ALTER TABLE reference.ref_holidays
    ADD COLUMN IF NOT EXISTS updated_at    timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS amended_by    text,   -- proclamation that changed this row (NULL = as first declared)
    ADD COLUMN IF NOT EXISTS previous_type text    -- holiday_type before the last amendment
        CHECK (previous_type IS NULL OR previous_type IN
               ('regular', 'special_non_working', 'special_working', 'federal', 'company'));

-- 3. History (append-only copy of the OLD row on every UPDATE / DELETE)
CREATE TABLE IF NOT EXISTS reference.ref_holidays_history (
    history_id   bigserial   PRIMARY KEY,
    op           text        NOT NULL CHECK (op IN ('UPDATE', 'DELETE')),
    calendar     text        NOT NULL,
    holiday_date date        NOT NULL,
    old_row      jsonb       NOT NULL,
    new_row      jsonb,
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   text        NOT NULL DEFAULT current_user
);
ALTER TABLE reference.ref_holidays_history ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS ref_holidays_history_date_idx
    ON reference.ref_holidays_history (calendar, holiday_date, changed_at);

CREATE OR REPLACE FUNCTION reference.fn_ref_holidays_history()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO reference.ref_holidays_history (op, calendar, holiday_date, old_row, new_row)
        VALUES ('DELETE', OLD.calendar, OLD.holiday_date, to_jsonb(OLD), NULL);
        RETURN OLD;
    END IF;
    -- UPDATE: stamp updated_at; remember the prior type when it changes (unless the
    -- statement set previous_type itself).
    NEW.updated_at := now();
    IF NEW.holiday_type IS DISTINCT FROM OLD.holiday_type
       AND NEW.previous_type IS NOT DISTINCT FROM OLD.previous_type THEN
        NEW.previous_type := OLD.holiday_type;
    END IF;
    INSERT INTO reference.ref_holidays_history (op, calendar, holiday_date, old_row, new_row)
    VALUES ('UPDATE', OLD.calendar, OLD.holiday_date, to_jsonb(OLD), to_jsonb(NEW));
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ref_holidays_history ON reference.ref_holidays;
CREATE TRIGGER trg_ref_holidays_history
    BEFORE UPDATE OR DELETE ON reference.ref_holidays
    FOR EACH ROW EXECUTE FUNCTION reference.fn_ref_holidays_history();

-- 4. Watcher run log
CREATE TABLE IF NOT EXISTS pipeline.holiday_watch_runs (
    run_id            bigserial   PRIMARY KEY,
    ran_at            timestamptz NOT NULL DEFAULT now(),
    status            text        NOT NULL CHECK (status IN ('ok', 'findings', 'error')),
    og_items_scanned  integer     NOT NULL DEFAULT 0,   -- Official Gazette RSS items read this run
    og_watermark      integer,                          -- highest proclamation number reviewed (any year)
    og_watermark_year integer,
    nager_dates       integer     NOT NULL DEFAULT 0,   -- Nager.Date PH dates compared
    findings          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    emailed           boolean     NOT NULL DEFAULT false,
    error             text,
    dry_run           boolean     NOT NULL DEFAULT false
);
ALTER TABLE pipeline.holiday_watch_runs ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS holiday_watch_runs_ran_at_idx ON pipeline.holiday_watch_runs (ran_at DESC);

-- 5. Semantic layer
UPDATE agent.schema_metadata
   SET description = description ||
       ' Key is (calendar, holiday_date): a proclamation that changes a day''s type is an UPDATE (previous_type + amended_by record it; every change is copied to reference.ref_holidays_history by trigger).',
       updated_at = now()
 WHERE schema_name = 'reference' AND table_name = 'ref_holidays' AND column_name IS NULL
   AND description NOT LIKE '%ref_holidays_history%';

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, related_tables)
SELECT 'reference', 'ref_holidays_history', NULL,
  'Append-only audit of reference.ref_holidays: the OLD row (and NEW row for updates) on every UPDATE/DELETE, written by trigger trg_ref_holidays_history.',
  'Shows when and how a holiday was amended (e.g. special non-working -> regular) and by which migration/user.',
  array['reference.ref_holidays']::text[]
WHERE NOT EXISTS (SELECT 1 FROM agent.schema_metadata WHERE schema_name='reference' AND table_name='ref_holidays_history' AND column_name IS NULL);

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, related_tables)
SELECT 'pipeline', 'holiday_watch_runs', NULL,
  'One row per run of holiday_feed_watcher.py: Official Gazette proclamations scanned, watermark (highest proclamation number reviewed), Nager.Date cross-check, findings emailed. The watcher never edits ref_holidays; it emails proposed SQL for confirmation.',
  'Weekly detection of new / amended Philippine holiday proclamations so ref_holidays does not go stale silently.',
  array['reference.ref_holidays']::text[]
WHERE NOT EXISTS (SELECT 1 FROM agent.schema_metadata WHERE schema_name='pipeline' AND table_name='holiday_watch_runs' AND column_name IS NULL);

COMMIT;

NOTIFY pgrst, 'reload schema';
