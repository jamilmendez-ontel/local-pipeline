-- migrations/059_report_group_meta.sql
-- Adds reference.report_group_meta — presentation metadata per
-- (report_name, report_group). Previously this lived hardcoded in
-- open-items-report/src/queries.py REPORT_GROUPS dict, which meant a
-- code commit was needed every time a new carrier/region group was
-- onboarded. With this table, adding a new group is one INSERT row.

CREATE TABLE IF NOT EXISTS reference.report_group_meta (
    report_name    TEXT NOT NULL,
    report_group   TEXT NOT NULL,
    title          TEXT NOT NULL,
    file_label     TEXT NOT NULL,
    cadence        TEXT NOT NULL CHECK (cadence IN ('monday','tuesday','wednesday','thursday','friday','saturday','sunday','daily','manual')),
    logo_filename  TEXT,
    sort_order     INT  NOT NULL DEFAULT 100,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_name, report_group)
);

COMMENT ON TABLE reference.report_group_meta IS
'Per-(report_name, report_group) presentation metadata used by the Open Items Report (and future report generators) to render workbook titles, file names, carrier logos, and scheduling cadence.';
COMMENT ON COLUMN reference.report_group_meta.title         IS 'Banner text shown in row 6 of the SUMMARY sheet (e.g. "VZW/CGC - NSB Macro").';
COMMENT ON COLUMN reference.report_group_meta.file_label    IS 'File-name fragment (e.g. "TSC CGC NSB Macro"). Final filename = "Weekly Open Items Report - {file_label} {YYYYMMDD}.xlsx".';
COMMENT ON COLUMN reference.report_group_meta.cadence       IS 'Day of week the report runs (monday / friday / daily / manual). Drives Apps Script triggers.';
COMMENT ON COLUMN reference.report_group_meta.logo_filename IS 'Filename inside report-automation/open-items-report/reference/ for the carrier logo (e.g. "Verizon logo.png").';
COMMENT ON COLUMN reference.report_group_meta.sort_order    IS 'Optional ordering hint for --all generation; lower runs first.';

-- Seed the 12 currently-configured open_items_report groups.
INSERT INTO reference.report_group_meta
  (report_name, report_group, title, file_label, cadence, logo_filename, sort_order)
VALUES
  ('open_items_report', 'AT&T KS',           'AT&T/KS',              'TSC AT&T KS',         'monday', 'AT&T logo.jpg',     10),
  ('open_items_report', 'AT&T NE',           'AT&T/NE',              'TSC AT&T NE',         'monday', 'AT&T logo.jpg',     11),
  ('open_items_report', 'AT&T OH',           'AT&T/OH',              'TSC AT&T OH',         'monday', 'AT&T logo.jpg',     12),
  ('open_items_report', 'AT&T NTX',          'AT&T/NTX',             'TSC AT&T NTX',        'monday', 'AT&T logo.jpg',     13),
  ('open_items_report', 'AT&T STX',          'AT&T/STX',             'TSC AT&T STX',        'monday', 'AT&T logo.jpg',     14),
  ('open_items_report', 'TMO BAWA',          'TMO/BAWA',             'TSC BETA - TMO-BAWA', 'monday', 'T-Mobile logo.png', 20),
  ('open_items_report', 'TMO FL Excalibur',  'TMO/FL',               'TSC BETA - TMO-FL',   'monday', 'T-Mobile logo.png', 21),
  ('open_items_report', 'TMO GA Overlay',    'TMO/GA',               'TSC BETA - TMO-GA',   'monday', 'T-Mobile logo.png', 22),
  ('open_items_report', 'TMO PA Overlay',    'TMO/PA',               'TSC BETA - TMO-PA',   'monday', 'T-Mobile logo.png', 23),
  ('open_items_report', 'TMO SFL Excalibur', 'TMO/SFL',              'TSC BETA - TMO-SFL',  'monday', 'T-Mobile logo.png', 24),
  ('open_items_report', 'TMO UPNY Overlay',  'TMO/UPNY',             'TSC BETA - TMO-UPNY', 'monday', 'T-Mobile logo.png', 25),
  ('open_items_report', 'CGC NSB Macro',     'VZW/CGC - NSB Macro',  'TSC CGC NSB Macro',   'friday', 'Verizon logo.png',  30)
ON CONFLICT (report_name, report_group) DO UPDATE SET
  title         = EXCLUDED.title,
  file_label    = EXCLUDED.file_label,
  cadence       = EXCLUDED.cadence,
  logo_filename = EXCLUDED.logo_filename,
  sort_order    = EXCLUDED.sort_order,
  updated_at    = now();
