-- =============================================================================
-- 279_ar_invoice_activity_views.sql
-- Site / invoicing activity per invoice for the Daily Finance dashboard
-- (Jamil, 2026-10-12: "include a column that shows the site name / invoicing
-- activity ... like the column we have in the Summary of Overdue tables").
--
-- The "Activity" text in the finance team's Summary of Overdue sheet
-- ("D-HBR220 - Carrier COP + Swift Projects") is the QuickBooks invoice memo.
-- stg_ar_aging has no memo column; stg_sales_detail.memo_description carries
-- it, keyed by invoice number. This view folds the sales-detail lines of each
-- invoice into one row so the Apps Script dashboard can attach the text to the
-- AR Aging / Pending PO / drill-down invoice tables with one cached PostgREST
-- call (PostgREST cannot join stg_ar_aging to stg_sales_detail: no FK).
--
-- Two views:
-- 1. analytics.v_ar_invoice_activity: one row per invoice number (num). Multi-line invoices (29 of the 1,673
-- open invoices on the 2026-10-08 AR snapshot) join their distinct memos with
-- ' | '. Two invoice numbers appear under two customer spellings in sales detail;
-- grouping by num alone keeps them to one row. Invoices billed before the
-- sales-detail feed started (first as_of_date 2025-09-23) have no row and show
-- '—' on the dashboard: 41 of the 1,673 open invoices on 2026-10-08.
--
-- Preflight 2026-10-12 (voqfjfngdpcvevbkikud): 279 free (dir + open PR #81 holds
-- 272). New object, nothing depends on it. stg_sales_detail is 11,218 rows /
-- 10,073 invoices (incremental feed, not a daily snapshot), so the GROUP BY is a
-- single small hash aggregate. Coverage on the latest AR snapshot: 1,632 / 1,673
-- open invoices match by num (identical count when also matching customer).
-- GRANTs mirror analytics.v_ar_dates (anon + authenticated SELECT), which the
-- dashboard already reads with the same key.
--
-- APPLIED + VERIFIED 2026-10-12 ET against voqfjfngdpcvevbkikud via the Supabase MCP
-- (execute_sql, single transaction). Post-apply: 10,073 view rows; 1,632 of the
-- 1,673 open invoices on the latest AR snapshot join by num; anon/authenticated
-- SELECT granted; 5 agent.schema_metadata rows; spot-check 60283 = "FB-HCS514 -
-- Carrier COP + Swift Projects", 59203 = "D-HBR220 - ..." (match the finance sheet).
--
-- Second transaction (v_ar_aging_invoices) applied the same way right after;
-- 1,683 rows for as_of_date 2026-10-08, 3 more schema_metadata rows.
--
-- ROLLBACK:
--   DROP VIEW IF EXISTS analytics.v_ar_aging_invoices;
--   DROP VIEW IF EXISTS analytics.v_ar_invoice_activity;
--   DELETE FROM agent.schema_metadata
--    WHERE schema_name = 'analytics'
--      AND table_name IN ('v_ar_invoice_activity', 'v_ar_aging_invoices');
--   (then point the dashboard's DataService.gs fetches back at stg_ar_aging)
-- =============================================================================
BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE OR REPLACE VIEW analytics.v_ar_invoice_activity AS
SELECT
    num,
    string_agg(DISTINCT memo_description, ' | ' ORDER BY memo_description) AS activity,
    count(*)::integer                                                     AS line_count,
    max(as_of_date)                                                       AS last_seen_as_of
FROM data_staging.stg_sales_detail
WHERE transaction_type = 'Invoice'
  AND num IS NOT NULL
  AND memo_description IS NOT NULL
  AND btrim(memo_description) <> ''
GROUP BY num;

COMMENT ON VIEW analytics.v_ar_invoice_activity IS
  'One row per QuickBooks invoice number: its line memo(s) from stg_sales_detail joined with " | ". The Daily Finance dashboard attaches activity to stg_ar_aging invoice rows by num. Invoices older than the sales-detail feed (2025-09-23) have no row.';

GRANT SELECT ON analytics.v_ar_invoice_activity TO anon, authenticated, service_role;

INSERT INTO agent.schema_metadata
    (schema_name, table_name, column_name, description, business_context, example_values, data_notes, related_tables)
VALUES
    ('analytics', 'v_ar_invoice_activity', NULL,
     'Site / invoicing activity text per invoice number, folded from the QuickBooks sales-detail line memos.',
     'Daily Finance dashboard: the Activity column on the AR Aging, Pending PO and customer drill-down invoice tables (same text as the finance team''s Summary of Overdue sheet).',
     NULL,
     'Join to data_staging.stg_ar_aging on num. Multi-line invoices join distinct memos with " | " (line_count > 1). No row for invoices billed before 2025-09-23.',
     ARRAY['data_staging.stg_sales_detail', 'data_staging.stg_ar_aging']),
    ('analytics', 'v_ar_invoice_activity', 'num',
     'QuickBooks invoice number (text), matches stg_ar_aging.num.', NULL, ARRAY['60283', '59203'], NULL, NULL),
    ('analytics', 'v_ar_invoice_activity', 'activity',
     'Distinct line memos of the invoice joined with " | ", ordered alphabetically.', NULL,
     ARRAY['D-HBR220 - Carrier COP + Swift Projects', 'FB-HCS514 - Carrier COP + Swift Projects'], NULL, NULL),
    ('analytics', 'v_ar_invoice_activity', 'line_count',
     'Number of sales-detail lines with a memo on the invoice.', NULL, ARRAY['1', '2'], NULL, NULL),
    ('analytics', 'v_ar_invoice_activity', 'last_seen_as_of',
     'Latest sales-detail as_of_date carrying the invoice.', NULL, NULL, NULL, NULL);

-- 2. stg_ar_aging + activity, for the dashboard's invoice fetches ------------------
CREATE OR REPLACE VIEW analytics.v_ar_aging_invoices AS
SELECT
    a.id, a.as_of_date, a.email_received_date, a.aging_bucket, a.date,
    a.transaction_type, a.num, a.customer, a.location, a.due_date, a.amount,
    a.open_balance, a.past_due, a.po_number,
    v.activity,
    v.line_count AS activity_line_count
FROM data_staging.stg_ar_aging a
LEFT JOIN analytics.v_ar_invoice_activity v ON v.num = a.num;

COMMENT ON VIEW analytics.v_ar_aging_invoices IS
  'stg_ar_aging rows plus the invoice activity text (v_ar_invoice_activity, joined on num). Read by the Daily Finance dashboard for the AR Aging, Pending PO and drill-down invoice tables; filter by as_of_date + transaction_type exactly as on the base table.';

GRANT SELECT ON analytics.v_ar_aging_invoices TO anon, authenticated, service_role;

INSERT INTO agent.schema_metadata
    (schema_name, table_name, column_name, description, business_context, example_values, data_notes, related_tables)
VALUES
    ('analytics', 'v_ar_aging_invoices', NULL,
     'data_staging.stg_ar_aging with two extra columns: activity (site / invoicing activity memo) and activity_line_count.',
     'Daily Finance dashboard invoice tables. Same grain and filters as stg_ar_aging (one row per invoice per as_of_date snapshot).',
     NULL,
     'LEFT JOIN, so invoices without sales-detail lines (billed before 2025-09-23) have NULL activity. Filter by as_of_date and transaction_type = ''Invoice'' as on the base table.',
     ARRAY['data_staging.stg_ar_aging', 'analytics.v_ar_invoice_activity']),
    ('analytics', 'v_ar_aging_invoices', 'activity',
     'Site / invoicing activity text from v_ar_invoice_activity; NULL when the invoice has no sales-detail line.', NULL,
     ARRAY['D-HBR220 - Carrier COP + Swift Projects'], NULL, NULL),
    ('analytics', 'v_ar_aging_invoices', 'activity_line_count',
     'Number of sales-detail memo lines folded into activity; NULL when none.', NULL, ARRAY['1', '2'], NULL, NULL);

COMMIT;
