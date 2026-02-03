-- migrations/005_forms_ts18.sql
-- Add raw table for QA Form TS18

CREATE TABLE IF NOT EXISTS raw_form_qa_ts18 (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts18_run_id ON raw_form_qa_ts18(run_id);
CREATE INDEX IF NOT EXISTS idx_raw_form_qa_ts18_data ON raw_form_qa_ts18 USING GIN(data);
