-- Sales by Product/Service Detail tables
-- Raw + staging for QuickBooks Sales by Product/Service Detail export (Gmail attachment)

-- data_raw.raw_sales_detail
CREATE TABLE IF NOT EXISTS data_raw.raw_sales_detail (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    run_id          uuid NOT NULL,
    as_of_date      date NOT NULL,
    email_received_date timestamptz,
    source_file     text NOT NULL,
    data            jsonb NOT NULL
);
CREATE INDEX idx_raw_sales_detail_run_id ON data_raw.raw_sales_detail(run_id);
CREATE INDEX idx_raw_sales_detail_as_of_date ON data_raw.raw_sales_detail(as_of_date);

-- data_staging.stg_sales_detail
CREATE TABLE IF NOT EXISTS data_staging.stg_sales_detail (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of_date      date NOT NULL,
    email_received_date timestamptz,
    date            date,
    transaction_type text,
    num             text,
    customer        text,
    memo_description text,
    qty             integer,
    sales_price     numeric,
    amount          numeric,
    balance         numeric,
    po_number       text,
    service_date    date,
    run_id          uuid NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_stg_sales_detail_as_of_date ON data_staging.stg_sales_detail(as_of_date);
CREATE INDEX idx_stg_sales_detail_customer ON data_staging.stg_sales_detail(customer);
CREATE INDEX idx_stg_sales_detail_run_id ON data_staging.stg_sales_detail(run_id);
