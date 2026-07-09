-- 165: COP Invoice Forecast vs Actuals
-- Ledger of assets that reached Final COP Complete on the homescreen
-- (analytics.v_user_priorities wipes nightly, so the backlog must be persisted
-- here), the excess table for invoices we did not forecast, a watermark so the
-- daily job can process every sales snapshot exactly once, and serving views.
-- Job lives in report-automation/cop-invoice-forecast/.
-- Design: report-automation/docs/superpowers/specs/2026-07-08-cop-invoice-forecast-design.md

BEGIN;

-- 1. Append-only forecast ledger. One row per asset, first_seen_date is the ET
-- date the asset first entered the ledger; rows are closed (invoiced=true),
-- never deleted.
CREATE TABLE data_staging.stg_cop_invoice_forecast (
    asset_did             text PRIMARY KEY,
    asset_id              text,
    asset_name            text NOT NULL,
    asset_name_norm       text NOT NULL,
    project_name          text,
    carrier_group         text,
    task_name_clean       text,
    homescreen_scheduled  timestamptz,
    first_seen_date       date NOT NULL,
    -- forecast amount
    service_rate_raw      text,
    service_rate_forecast numeric,
    rate_source           text CHECK (rate_source IN ('final_cop_invoiced', 'quote_provided')),
    invoicing_form_matched boolean NOT NULL DEFAULT false,
    -- lifecycle
    invoiced              boolean NOT NULL DEFAULT false,
    invoiced_date         date,
    invoiced_amount       numeric,
    invoice_num           text,
    invoiced_customer     text,
    matched_memo          text,
    closed_at             timestamptz,
    -- lineage
    load_run_id           uuid,
    source_system         text NOT NULL DEFAULT 'cop_invoice_forecast_job',
    extracted_at          timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_stg_cop_invoice_forecast_open
    ON data_staging.stg_cop_invoice_forecast (first_seen_date)
    WHERE invoiced = false;

-- 2. Excess: invoice lines that were not a normal forecast closure.
-- reason: no_match (no ledger row), same_day_appeared (ledger row first seen on
-- or after the invoice day; PROVISIONAL team rule, see design spec section 5),
-- already_closed (ledger row was closed by an earlier invoice).
CREATE TABLE data_staging.stg_cop_invoice_excess (
    excess_key           text PRIMARY KEY,  -- md5(as_of_date|num|memo|amount)
    as_of_date           date NOT NULL,
    email_received_date  timestamptz,
    service_date         date,
    invoice_date         date,
    memo_description     text,
    parsed_asset_name    text,
    amount               numeric,
    invoice_num          text,
    customer             text,
    po_number            text,
    matched_asset_did    text,
    reason               text NOT NULL CHECK (reason IN ('no_match', 'same_day_appeared', 'already_closed')),
    load_run_id          uuid,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_stg_cop_invoice_excess_day
    ON data_staging.stg_cop_invoice_excess (as_of_date);

-- 3. Watermark: last sales snapshot (as_of_date) fully processed. Single row.
CREATE TABLE pipeline.cop_invoice_forecast_state (
    id                    boolean PRIMARY KEY DEFAULT true CHECK (id),
    last_processed_as_of  date,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- 4. Serving views (small volumes; live views, no MVs).
CREATE VIEW analytics.v_cop_forecast_open AS
SELECT
    f.asset_did,
    f.asset_id,
    f.asset_name,
    f.project_name,
    f.carrier_group,
    f.first_seen_date,
    (now() AT TIME ZONE 'America/New_York')::date - f.first_seen_date AS days_open,
    f.service_rate_raw,
    f.service_rate_forecast,
    f.rate_source,
    f.invoicing_form_matched
FROM data_staging.stg_cop_invoice_forecast f
WHERE f.invoiced = false;

CREATE VIEW analytics.v_cop_invoiced AS
SELECT
    f.asset_did,
    f.asset_id,
    f.asset_name,
    f.project_name,
    f.carrier_group,
    f.first_seen_date,
    f.invoiced_date,
    f.invoiced_date - f.first_seen_date AS days_to_invoice,
    f.service_rate_forecast,
    f.invoiced_amount,
    f.invoiced_amount - f.service_rate_forecast AS variance_amount,
    f.invoice_num,
    f.invoiced_customer,
    f.matched_memo,
    f.closed_at
FROM data_staging.stg_cop_invoice_forecast f
WHERE f.invoiced = true;

CREATE VIEW analytics.v_cop_daily_summary AS
WITH closures AS (
    SELECT invoiced_date AS day,
           count(*) AS closed_count,
           sum(invoiced_amount) AS closed_amount,
           sum(service_rate_forecast) AS closed_forecast_amount
    FROM data_staging.stg_cop_invoice_forecast
    WHERE invoiced = true
    GROUP BY invoiced_date
), excess AS (
    SELECT as_of_date AS day,
           count(*) AS excess_count,
           sum(amount) AS excess_amount
    FROM data_staging.stg_cop_invoice_excess
    GROUP BY as_of_date
)
SELECT
    coalesce(c.day, e.day) AS day,
    coalesce(c.closed_count, 0) AS closed_count,
    coalesce(c.closed_amount, 0) AS closed_amount,
    coalesce(c.closed_forecast_amount, 0) AS closed_forecast_amount,
    coalesce(e.excess_count, 0) AS excess_count,
    coalesce(e.excess_amount, 0) AS excess_amount
FROM closures c
FULL OUTER JOIN excess e USING (day);

-- 5. Lockdown (repo standard: RLS deny-all, no anon/authenticated access).
ALTER TABLE data_staging.stg_cop_invoice_forecast ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_staging.stg_cop_invoice_excess   ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline.cop_invoice_forecast_state   ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON data_staging.stg_cop_invoice_forecast FROM anon, authenticated;
REVOKE ALL ON data_staging.stg_cop_invoice_excess   FROM anon, authenticated;
REVOKE ALL ON pipeline.cop_invoice_forecast_state   FROM anon, authenticated;
REVOKE ALL ON analytics.v_cop_forecast_open  FROM anon, authenticated;
REVOKE ALL ON analytics.v_cop_invoiced       FROM anon, authenticated;
REVOKE ALL ON analytics.v_cop_daily_summary  FROM anon, authenticated;

-- 6. Register in the semantic layer (ship gate).
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('data_staging', 'stg_cop_invoice_forecast',
   'COP invoice forecast ledger. One row per asset that reached Final COP Complete on the Swift homescreen (analytics.v_user_priorities). Append-only; invoiced=true closes a row, nothing is deleted. first_seen_date is the ET date the asset entered the ledger. service_rate_forecast is parsed from stg_invoicing_form (numbered Final COP Invoiced task rate, Quote Provided fallback; raw text kept in service_rate_raw).',
   'Written daily by the cop-invoice-forecast job in report-automation. Feeds the daily forecast-vs-actual invoice report to accounting.',
   ARRAY['data_staging.stg_cop_invoice_excess', 'data_staging.stg_invoicing_form', 'data_staging.stg_sales_detail']),
  ('data_staging', 'stg_cop_invoice_excess',
   'Invoice lines from stg_sales_detail that were not a normal forecast closure. reason: no_match (no ledger row), same_day_appeared (provisional team rule), already_closed (second invoice for an already-closed asset).',
   'Review/QA surface for the COP invoice forecast report.',
   ARRAY['data_staging.stg_cop_invoice_forecast', 'data_staging.stg_sales_detail']),
  ('pipeline', 'cop_invoice_forecast_state',
   'Single-row watermark: last stg_sales_detail as_of_date processed by the cop-invoice-forecast job. NULL row absent means the job has never seeded.',
   'Lets the daily job process every sales snapshot exactly once, including catch-up after skipped days.',
   ARRAY['data_staging.stg_sales_detail']),
  ('analytics', 'v_cop_forecast_open',
   'Open COP invoice forecast backlog with days_open and forecast rate.',
   'Serving view for the COP invoice forecast report.',
   ARRAY['data_staging.stg_cop_invoice_forecast']),
  ('analytics', 'v_cop_invoiced',
   'Closed (invoiced) COP forecast ledger rows with forecast-vs-actual variance.',
   'Serving view for the COP invoice forecast report.',
   ARRAY['data_staging.stg_cop_invoice_forecast']),
  ('analytics', 'v_cop_daily_summary',
   'Per-day closure and excess KPIs for the COP invoice forecast report.',
   'Serving view for the COP invoice forecast report.',
   ARRAY['data_staging.stg_cop_invoice_forecast', 'data_staging.stg_cop_invoice_excess']);

COMMIT;
