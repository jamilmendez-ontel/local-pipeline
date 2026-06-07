-- 065_invoicing_form_tables.sql
-- Quote Automation Phase 0: invoicing form raw + staging tables.
-- Raw: one table, faithful, one row per requirement-response, form_did column.
-- Staging: flat typed columns + extra_fields jsonb overflow (future forms never break loads).

-- ---- RAW ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_raw.raw_invoicing_form (
    id         BIGSERIAL PRIMARY KEY,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id     UUID NOT NULL,
    form_did   TEXT NOT NULL,
    data       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_invoicing_form_run_id   ON data_raw.raw_invoicing_form(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_invoicing_form_form_did ON data_raw.raw_invoicing_form(form_did);
CREATE INDEX IF NOT EXISTS idx_raw_invoicing_form_data     ON data_raw.raw_invoicing_form USING GIN(data);

-- ---- STAGING ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_staging.stg_invoicing_form (
    id                  BIGSERIAL PRIMARY KEY,
    form_did            TEXT NOT NULL,
    -- known typed fields (current 4 forms)
    project             TEXT,
    site_name           TEXT,
    site_id             TEXT,
    task                TEXT,
    requirement         TEXT,
    requirement_status  TEXT,
    sow                 TEXT,
    invoice_category    TEXT,
    service_rate        TEXT,      -- free-text in source; cast to numeric in views
    ll_cop              TEXT,
    landlord            TEXT,
    landlord_others     TEXT,
    pmi_cop             TEXT,
    rf_mitigation_cop   TEXT,
    -- derived
    fa_number           TEXT,
    site_name_norm      TEXT,
    -- overflow for any unmapped key from any (future) form
    extra_fields        JSONB,
    run_id              UUID NOT NULL,
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stg_invoicing_form_fa_name ON data_staging.stg_invoicing_form(fa_number, site_name_norm);
CREATE INDEX IF NOT EXISTS idx_stg_invoicing_form_task    ON data_staging.stg_invoicing_form(task);
CREATE INDEX IF NOT EXISTS idx_stg_invoicing_form_run_id  ON data_staging.stg_invoicing_form(run_id);
