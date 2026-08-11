-- Migration 231: reference.ref_qa_forms — QA form registry (replaces config.py QA_FORMS)
-- New TS projects get their QA form auto-registered by the nightly forms pipeline
-- (see docs/superpowers/specs/2026-08-11-ts-project-auto-coverage-design.md).

CREATE TABLE reference.ref_qa_forms (
    ts_number     INTEGER PRIMARY KEY CHECK (ts_number >= 13),
    form_id       TEXT NOT NULL UNIQUE,
    form_title    TEXT NOT NULL,
    table_name    TEXT NOT NULL UNIQUE,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    registered_by TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE reference.ref_qa_forms ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON reference.ref_qa_forms FROM anon, authenticated;
GRANT ALL ON reference.ref_qa_forms TO service_role;

INSERT INTO reference.ref_qa_forms (ts_number, form_id, form_title, table_name, registered_by) VALUES
    (13, '-NH1hUPkaKtPdd7BK9cb', 'ACTIVE - QA Form TS13', 'raw_form_qa_ts13', 'seed'),
    (14, '-NXCg4vTDNVykN8ioMYp', 'ACTIVE - QA Form TS14', 'raw_form_qa_ts14', 'seed'),
    (15, '-Np6o9OCL4RWIJq68HJe', 'ACTIVE - QA Form TS15', 'raw_form_qa_ts15', 'seed'),
    (16, '-O9ACLN3je1w7oEoG5hY', 'ACTIVE - QA Form TS16', 'raw_form_qa_ts16', 'seed'),
    (17, '-ONMD-cGBq-_3r9ybaAq', 'ACTIVE - QA Form TS17', 'raw_form_qa_ts17', 'seed'),
    (18, '-O_J2hPlryTezP9RhujA', 'ACTIVE - QA Form TS18', 'raw_form_qa_ts18', 'seed'),
    (19, '-Omun_NWXeQE1tEhSPXf', 'ACTIVE - QA Form TS19', 'raw_form_qa_ts19', 'seed');
