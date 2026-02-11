-- migrations/016_ar_aging_tables.sql
-- Raw and staging tables for AR Aging data (QuickBooks export via Gmail)

-- ============================================================
-- RAW TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS data_raw.raw_ar_aging (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    run_id          uuid NOT NULL,
    as_of_date      date NOT NULL,
    email_received_date timestamptz,
    source_file     text NOT NULL,
    data            jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_ar_aging_run_id
    ON data_raw.raw_ar_aging(run_id);

CREATE INDEX IF NOT EXISTS idx_raw_ar_aging_as_of_date
    ON data_raw.raw_ar_aging(as_of_date);

-- ============================================================
-- STAGING TABLE
-- ============================================================

CREATE TABLE IF NOT EXISTS data_staging.stg_ar_aging (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of_date      date NOT NULL,
    email_received_date timestamptz,
    aging_bucket    text,
    date            date,
    transaction_type text,
    num             text,
    customer        text,
    location        text,
    due_date        date,
    amount          numeric,
    open_balance    numeric,
    past_due        integer,
    po_number       text,
    run_id          uuid NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stg_ar_aging_as_of_date
    ON data_staging.stg_ar_aging(as_of_date);

CREATE INDEX IF NOT EXISTS idx_stg_ar_aging_customer
    ON data_staging.stg_ar_aging(customer);

CREATE INDEX IF NOT EXISTS idx_stg_ar_aging_run_id
    ON data_staging.stg_ar_aging(run_id);

CREATE INDEX IF NOT EXISTS idx_stg_ar_aging_aging_bucket
    ON data_staging.stg_ar_aging(aging_bucket);
