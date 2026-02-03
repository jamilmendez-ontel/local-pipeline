-- migrations/004_forms_tables.sql
-- Raw and staging tables for Forms data

-- ============================================================
-- RAW TABLES (one per form type)
-- ============================================================

-- QA Forms (TS13+)
CREATE TABLE IF NOT EXISTS raw_form_qa_ts13 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_form_qa_ts14 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_form_qa_ts15 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_form_qa_ts16 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_form_qa_ts17 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

-- Indexes for raw tables
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts13_run_id ON raw_form_qa_ts13(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts14_run_id ON raw_form_qa_ts14(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts15_run_id ON raw_form_qa_ts15(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts16_run_id ON raw_form_qa_ts16(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts17_run_id ON raw_form_qa_ts17(run_id);

CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts13_data ON raw_form_qa_ts13 USING GIN(data);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts14_data ON raw_form_qa_ts14 USING GIN(data);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts15_data ON raw_form_qa_ts15 USING GIN(data);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts16_data ON raw_form_qa_ts16 USING GIN(data);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts17_data ON raw_form_qa_ts17 USING GIN(data);

-- ============================================================
-- STAGING TABLE (combined QA forms)
-- ============================================================

CREATE TABLE IF NOT EXISTS stg_qa_form (
    id BIGSERIAL PRIMARY KEY,
    -- Source tracking
    form_name TEXT NOT NULL,
    form_id TEXT NOT NULL,
    -- Project info
    project TEXT,
    project_number INTEGER,
    site_name TEXT,
    site_id TEXT,
    task TEXT,
    requirement TEXT,
    requirement_status TEXT,
    -- QA fields
    live_review_performed TEXT,
    swift_used_for_photos TEXT,
    crew_lead TEXT,
    -- AAT
    aat TEXT,
    aat_issues TEXT,
    aat_other_issues TEXT,
    -- RET
    ret TEXT,
    ret_issues TEXT,
    ret_other_issues TEXT,
    -- Sweeps
    sweeps TEXT,
    sweeps_issues TEXT,
    sweeps_other_issues TEXT,
    -- PIM
    pim TEXT,
    pim_issues TEXT,
    pim_other_issues TEXT,
    -- Fiber
    fiber TEXT,
    fiber_issues TEXT,
    fiber_other_issues TEXT,
    -- Pictures
    pictures TEXT,
    pictures_issues TEXT,
    pictures_other_issues TEXT,
    -- As-Builts
    as_builts TEXT,
    as_builts_issues TEXT,
    as_builts_other_issues TEXT,
    -- RF Mitigation
    rf_mitigation TEXT,
    rf_mitigation_issues TEXT,
    rf_mitigation_other_issues TEXT,
    -- Landlord / Tower Owner
    landlord_tower_owner TEXT,
    landlord_tower_owner_issues TEXT,
    -- Permits
    permits TEXT,
    -- Additional Documents
    additional_documents TEXT,
    -- PMI
    pmi TEXT,
    pmi_vendor TEXT,
    pmi_others_vendor TEXT,
    pmi_mount_modification_required TEXT,
    pmi_issues TEXT,
    pmi_other_issues TEXT,
    pmi_report_received TEXT,
    -- Power Testing
    power_testing TEXT,
    power_testing_issues TEXT,
    power_testing_other_issues TEXT,
    -- Connectivity Testing
    connectivity_testing TEXT,
    connectivity_testing_issues TEXT,
    connectivity_testing_other_issues TEXT,
    -- Optical Power Testing
    optical_power_testing TEXT,
    optical_power_testing_other_issues TEXT,
    -- Restoration
    restoration TEXT,
    -- NA Checklist
    na_checklist TEXT,
    na_checklist_issues TEXT,
    na_checklist_other_issues TEXT,
    -- Metadata
    run_id UUID NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for staging table
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_project ON stg_qa_form(project);
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_project_number ON stg_qa_form(project_number);
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_form_name ON stg_qa_form(form_name);
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_requirement_status ON stg_qa_form(requirement_status);
CREATE INDEX IF NOT EXISTS idx_stg_qa_form_run_id ON stg_qa_form(run_id);
