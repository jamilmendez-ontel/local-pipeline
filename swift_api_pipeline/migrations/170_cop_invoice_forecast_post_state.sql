-- 170: COP Invoice Forecast: post-dedupe state
-- Supports re-running the report when a corrected Daily Revenue Report email
-- replaces a snapshot (same as_of_date, new content) and posting at
-- max(5am ET, email arrival): the job reprocesses the watermark day
-- idempotently and posts only when the latest day is new or its snapshot
-- content fingerprint changed.
-- Job: report-automation/cop-invoice-forecast/.

BEGIN;

ALTER TABLE pipeline.cop_invoice_forecast_state
    ADD COLUMN last_posted_as_of date,
    ADD COLUMN last_posted_fingerprint text;

UPDATE agent.schema_metadata
SET description = 'Single-row state for the cop-invoice-forecast job: last_processed_as_of = last stg_sales_detail as_of_date processed (watermark; the watermark day itself is reprocessed idempotently so corrected same-day revenue emails update the report); last_posted_as_of + last_posted_fingerprint dedupe Chat posts (post only when the latest day is new or its snapshot content changed).',
    updated_at = now()
WHERE schema_name = 'pipeline' AND table_name = 'cop_invoice_forecast_state';

COMMIT;
