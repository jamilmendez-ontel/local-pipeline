-- 217: app_scrub schema — Scrub webapp OLTP state (spec: oot-scorecard/docs/specs/2026-08-03-scrub-webapp-design.md)
-- All tables RLS deny-all (standing rule 2026-08-03). App reads/writes via service_role;
-- pipeline via postgres. anon/authenticated get nothing.

CREATE SCHEMA app_scrub;
REVOKE USAGE ON SCHEMA app_scrub FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA app_scrub REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app_scrub REVOKE ALL ON TABLES FROM anon, authenticated;

-- Canonical field keys: 2 gate fields + 6 OOT milestones (spec §5)
CREATE TABLE app_scrub.field_keys (
    field_key text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('gate', 'milestone')),
    swift_task_name text NOT NULL
);
ALTER TABLE app_scrub.field_keys ENABLE ROW LEVEL SECURITY;
INSERT INTO app_scrub.field_keys (field_key, kind, swift_task_name) VALUES
    ('final_cop_complete',   'gate',      'Final COP Complete'),
    ('package_48hr_complete','gate',      '48Hr / Test Package Complete'),
    ('cutover_complete',     'milestone', 'Cutover Complete (A)'),
    ('raw_files_48hr',       'milestone', '48Hr / Test Package Raw Files Received'),
    ('ontel_notified_48hr',  'milestone', '48Hr / Test Package Ontel Notified'),
    ('cx_complete',          'milestone', 'CX Complete (A)'),
    ('raw_files_fcop',       'milestone', 'Final COP Raw Files Received'),
    ('ontel_notified_fcop',  'milestone', 'Final COP Ontel Notified');

CREATE TABLE app_scrub.site_date_overrides (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_did text NOT NULL,
    field_key text NOT NULL REFERENCES app_scrub.field_keys(field_key),
    override_date date NOT NULL,
    reason text NOT NULL,
    swift_value_at_override date,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    superseded_by bigint REFERENCES app_scrub.site_date_overrides(id)
);
ALTER TABLE app_scrub.site_date_overrides ENABLE ROW LEVEL SECURITY;
-- Spec §5 invariant: at most one ACTIVE override per (asset, field)
CREATE UNIQUE INDEX uq_active_override
    ON app_scrub.site_date_overrides (asset_did, field_key)
    WHERE superseded_at IS NULL;

CREATE TABLE app_scrub.scrub_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_did text NOT NULL,
    field_key text NOT NULL REFERENCES app_scrub.field_keys(field_key),
    action text NOT NULL CHECK (action IN ('confirm', 'correct')),
    actor text NOT NULL,
    acted_at timestamptz NOT NULL DEFAULT now(),
    swift_value date,
    email_value date,
    data_loaded_at timestamptz
);
ALTER TABLE app_scrub.scrub_log ENABLE ROW LEVEL SECURITY;
CREATE INDEX ix_scrub_log_asset ON app_scrub.scrub_log (asset_did, acted_at DESC);

CREATE TABLE app_scrub.app_user (
    email text PRIMARY KEY,
    display_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'lead')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app_scrub.app_user ENABLE ROW LEVEL SECURITY;
INSERT INTO app_scrub.app_user (email, display_name, role)
    VALUES ('jamil.mendez@ontel.co', 'Jamil Mendez', 'admin');

CREATE TABLE app_scrub.oot_eligibility (
    asset_did text PRIMARY KEY,
    became_eligible_on date NOT NULL,
    basis text NOT NULL CHECK (basis IN ('auto_match', 'scrubbed')),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app_scrub.oot_eligibility ENABLE ROW LEVEL SECURITY;

-- Reference layer (spec §5): holidays + workday function, our own copies
CREATE TABLE IF NOT EXISTS reference.ref_scrub_holidays (
    holiday_date date PRIMARY KEY,
    label text
);
ALTER TABLE reference.ref_scrub_holidays ENABLE ROW LEVEL SECURITY;
-- one-time read-only copy; scorecard.holidays' date column verified at execution to be
-- `holiday_date` (not `holiday` as originally drafted)
INSERT INTO reference.ref_scrub_holidays (holiday_date, label)
    SELECT h.holiday_date, NULL FROM scorecard.holidays h
    ON CONFLICT (holiday_date) DO NOTHING;

-- Workday function boundary verified against scorecard.fn_workdays via a 4-fixture parity
-- check (Mon-Fri same week / Fri-Mon weekend / spans a holiday / same day). scorecard's
-- version counts the half-open range [d1, d2) -- generate_series(d1, d2-1) -- so d_start is
-- INCLUDED and d_end is EXCLUDED. An initial draft used (d_start, d_end] and matched 3/4
-- fixtures but diverged on the holiday-spanning case (2026-07-03, a Friday holiday, sits on
-- the start boundary); switched to [d_start, d_end) to match exactly. Same-day
-- (d_start = d_end) returns 0. Unlike scorecard.fn_workdays, this does not sign-flip for a
-- reversed (d_end < d_start) range -- out of scope for the Scrub webapp's forward-only usage.
CREATE OR REPLACE FUNCTION reference.fn_workdays_scrub(d_start date, d_end date)
RETURNS integer LANGUAGE sql STABLE AS $$
    SELECT count(*)::int
    FROM generate_series(least(d_start, d_end), greatest(d_start, d_end) - 1, interval '1 day') g(day)
    WHERE extract(isodow FROM g.day) < 6
      AND g.day::date NOT IN (SELECT holiday_date FROM reference.ref_scrub_holidays);
$$;

-- AI semantic layer registration. agent.schema_metadata is a per-table/view catalog
-- (schema_name, table_name NOT NULL, column_name, description NOT NULL, business_context,
-- example_values, data_notes, related_tables, created_at, updated_at) verified at execution
-- via information_schema.columns — not the (schema_name, description)-only shape originally
-- drafted. One row per app_scrub table plus the new reference table; column_name left NULL
-- (table-level rows), matching the convention used by migration 206.
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables) VALUES
    ('app_scrub', 'field_keys',
     'Canonical scrub field keys: 2 gate fields (final_cop_complete, package_48hr_complete) and 6 OOT milestone fields, each mapped to its Swift task name.',
     'Lookup table driving which date fields the Scrub webapp scrubs and displays; referenced by site_date_overrides and scrub_log.',
     array['app_scrub.site_date_overrides', 'app_scrub.scrub_log']::text[]),
    ('app_scrub', 'site_date_overrides',
     'Manual corrections to a Swift-sourced date field on an asset. At most one ACTIVE row (superseded_at IS NULL) per (asset_did, field_key), enforced by unique index uq_active_override. Superseded rows are kept for history via superseded_at/superseded_by.',
     'Scrub webapp: a lead corrects a Swift date value; the override supersedes the prior active one instead of being deleted, preserving an audit trail.',
     array['app_scrub.field_keys', 'app_scrub.scrub_log']::text[]),
    ('app_scrub', 'scrub_log',
     'Append-only audit log of every confirm/correct action taken in the Scrub webapp, capturing the Swift value and email/reported value seen at the time of action.',
     'Scrub webapp activity trail per asset/field; drives the asset scrub history view.',
     array['app_scrub.field_keys', 'app_scrub.site_date_overrides']::text[]),
    ('app_scrub', 'app_user',
     'Allowlist of users permitted to sign in to the Scrub webapp, with role (admin/lead) and active flag.',
     'Gates Google sign-in: only allowlisted, active emails may access scrub.ontel.co.',
     NULL),
    ('app_scrub', 'oot_eligibility',
     'One row per asset once it becomes OOT (out-of-territory) eligible, recording the eligibility date and whether it was auto-matched from Swift data or established via manual scrub.',
     'Backs the OOT view in the Scrub webapp (Plan C).',
     array['app_scrub.site_date_overrides']::text[]),
    ('reference', 'ref_scrub_holidays',
     'Scrub webapp''s own copy of the company holiday calendar (date + optional label), seeded one-time from scorecard.holidays. Used by reference.fn_workdays_scrub to exclude holidays from workday counts.',
     'Keeps the Scrub webapp''s workday math independent of the scorecard schema, which we do not write to.',
     array['scorecard.holidays']::text[])
ON CONFLICT DO NOTHING;

NOTIFY pgrst, 'reload schema';
