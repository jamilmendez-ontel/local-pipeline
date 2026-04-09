-- Migration 028: Schema Metadata Enrichment
-- Closes gaps in agent.schema_metadata to improve AI query accuracy:
--   A. stg_ar_aging — table data_notes + 3 new columns + 6 column enrichments
--   B. stg_sales_detail — table data_notes + 3 new columns + 6 column enrichments
--   C. Lookup tables — qa_form_asset_did_lookup (5 rows) + carrier_group_lookup (5 rows)
--   D. Fixes & enrichments — requirement_status, past_due, carrier_group, row count, FKs
--
-- All INSERTs use ON CONFLICT ... DO UPDATE for idempotency.
-- All UPDATEs set updated_at = NOW().

-- ============================================================
-- SECTION A: stg_ar_aging enrichments
-- ============================================================

-- A1. Table-level UPDATE — add data_notes about append mode and QuickBooks source
UPDATE agent.schema_metadata
SET data_notes = 'Append-only: each pipeline run adds new rows from the latest QuickBooks AR Aging email. Rows are never updated or deleted. Source: QuickBooks desktop report emailed daily as CSV attachment.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name IS NULL;

-- A2. NEW column INSERTs: id, run_id, loaded_at
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_ar_aging', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY. Do not use in WHERE clauses for business logic.'),

('data_staging', 'stg_ar_aging', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_ar_aging', 'loaded_at',
 'Timestamp when this row was loaded into the database',
 'Pipeline internal. Use email_received_date for business time filtering instead.',
 'Defaults to NOW() at insert time. Always UTC.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- A3. Column UPDATEs — enrich existing stg_ar_aging columns
UPDATE agent.schema_metadata
SET example_values = ARRAY['Current', '1 - 30', '31 - 60', '61 - 90', '91 and over'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'aging_bucket';

UPDATE agent.schema_metadata
SET example_values = ARRAY['Invoice', 'Payment', 'Credit Memo', 'Journal Entry'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'transaction_type';

UPDATE agent.schema_metadata
SET business_context = 'When the automated email arrived from QuickBooks. Used for dedup: the pipeline only processes emails newer than the max email_received_date already loaded. User may say: "email date", "received date".',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'email_received_date';

UPDATE agent.schema_metadata
SET data_notes = 'Can be negative for credit memos and payments. Positive for invoices.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'amount';

UPDATE agent.schema_metadata
SET data_notes = 'Zero means fully paid. NULL should not occur. Sum open_balance for total AR outstanding.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'open_balance';

-- A4. BUG FIX: past_due is days, not dollars
UPDATE agent.schema_metadata
SET description = 'Days past due (integer)',
    business_context = 'Number of days past the due date. 0 means not overdue. User may say: "past due", "overdue days", "days late", "how overdue".',
    data_notes = 'Integer value representing days, NOT a dollar amount. Use aging_bucket for categorical grouping.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_ar_aging' AND column_name = 'past_due';


-- ============================================================
-- SECTION B: stg_sales_detail enrichments
-- ============================================================

-- B1. Table-level UPDATE — add data_notes about append mode and QuickBooks source
UPDATE agent.schema_metadata
SET data_notes = 'Append-only: each pipeline run adds new rows from the latest QuickBooks Sales Detail email. Rows are never updated or deleted. Source: QuickBooks desktop report emailed daily as CSV attachment.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name IS NULL;

-- B2. NEW column INSERTs: id, run_id, loaded_at
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'stg_sales_detail', 'id',
 'Auto-generated row identifier',
 'Pipeline internal. Not useful for business queries.',
 'BIGINT GENERATED ALWAYS AS IDENTITY. Do not use in WHERE clauses for business logic.'),

('data_staging', 'stg_sales_detail', 'run_id',
 'Pipeline run identifier',
 'Pipeline internal. Links to pipeline.pipeline_runs for debugging.',
 'UUID referencing the pipeline run that loaded this row.'),

('data_staging', 'stg_sales_detail', 'loaded_at',
 'Timestamp when this row was loaded into the database',
 'Pipeline internal. Use email_received_date for business time filtering instead.',
 'Defaults to NOW() at insert time. Always UTC.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- B3. Column UPDATEs — enrich existing stg_sales_detail columns
UPDATE agent.schema_metadata
SET example_values = ARRAY['Invoice', 'Sales Receipt', 'Credit Memo'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'transaction_type';

UPDATE agent.schema_metadata
SET business_context = 'When the automated email arrived from QuickBooks. Used for dedup: the pipeline only processes emails newer than the max email_received_date already loaded. User may say: "email date", "received date".',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'email_received_date';

UPDATE agent.schema_metadata
SET data_notes = 'Equals qty * sales_price. Can be negative for credit memos. Positive for invoices and sales receipts.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'amount';

UPDATE agent.schema_metadata
SET data_notes = 'Can be 0 for non-quantity items (e.g., discount lines, subtotal rows). NULL should not occur.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'qty';

UPDATE agent.schema_metadata
SET data_notes = 'Can be NULL for summary/subtotal rows that have no unit price. Represents price per unit in dollars.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'sales_price';

UPDATE agent.schema_metadata
SET data_notes = 'Running balance for the transaction. Decreases as payments are applied. Zero means fully paid.',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_sales_detail' AND column_name = 'balance';


-- ============================================================
-- SECTION C: Lookup table metadata (all NEW)
-- ============================================================

-- C1. qa_form_asset_did_lookup — 1 table-level + 4 column-level
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
VALUES
('data_staging', 'qa_form_asset_did_lookup', NULL,
 'Persistent lookup table for QA form asset_did mappings',
 'Internal pipeline table — not for direct user queries. Preserves site_id-to-asset_did mappings that would be lost during stg_qa_form truncate+reload.',
 'Cumulative: never loses established mappings. During each pipeline run, Pass 0 of backfill_asset_did() restores mappings from this table, and the Save step persists any new mappings back.',
 ARRAY['stg_qa_form (provides asset_did recovery)', 'stg_assets (source of asset_did)'])
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    related_tables = EXCLUDED.related_tables,
    updated_at = NOW();

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes)
VALUES
('data_staging', 'qa_form_asset_did_lookup', 'site_id',
 'Site identifier used as the primary key',
 'Matches stg_qa_form.site_id and stg_assets.asset_id. This is the lookup key.',
 'TEXT, NOT NULL, PRIMARY KEY. One row per unique site_id.'),

('data_staging', 'qa_form_asset_did_lookup', 'site_name',
 'Human-readable site name for reference',
 'Matches stg_qa_form.site_name. Nullable — stored for debugging, not used in lookups.',
 NULL),

('data_staging', 'qa_form_asset_did_lookup', 'asset_did',
 'Immutable asset identifier mapped to this site_id',
 'The resolved asset_did from stg_assets. Used to restore stg_qa_form.asset_did after truncate+reload.',
 'TEXT, NOT NULL. Once set, should not change for a given site_id.'),

('data_staging', 'qa_form_asset_did_lookup', 'updated_at',
 'Timestamp of last update to this mapping',
 'Pipeline internal. Tracks when the mapping was last confirmed or updated.',
 'Defaults to NOW(). Updated on each UPSERT.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- C2. carrier_group_lookup — 1 table-level + 4 column-level
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, data_notes, related_tables)
VALUES
('data_staging', 'carrier_group_lookup', NULL,
 'Lookup table mapping search terms to carrier groups',
 'Maps keywords found in asset_id to carrier groups (Verizon, AT&T/DISH, TMO/USCC) for COP reporting. Used to backfill stg_assets.carrier_group.',
 'Pattern matching uses ILIKE against asset_id. First match wins via match_order. 10 search terms map to 3 carrier groups.',
 ARRAY['stg_assets (backfills carrier_group via asset_id pattern matching)'])
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    data_notes = EXCLUDED.data_notes,
    related_tables = EXCLUDED.related_tables,
    updated_at = NOW();

INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, example_values, data_notes)
VALUES
('data_staging', 'carrier_group_lookup', 'id',
 'Auto-generated row identifier',
 'Pipeline internal.',
 NULL,
 'SERIAL PRIMARY KEY.'),

('data_staging', 'carrier_group_lookup', 'search_term',
 'Keyword to match against asset_id using ILIKE pattern',
 'If asset_id contains this term (case-insensitive), the asset is assigned the corresponding carrier_group.',
 ARRAY['VZW', 'Verizon', 'DISH', 'AT&T', 'T-Mobile', 'US Cellular'],
 'TEXT, NOT NULL, UNIQUE. Each term maps to exactly one carrier_group.'),

('data_staging', 'carrier_group_lookup', 'carrier_group',
 'The carrier group label assigned when this search_term matches',
 'Three possible values. Used for COP reporting and filtering.',
 ARRAY['Verizon', 'AT&T/DISH', 'TMO/USCC'],
 'TEXT, NOT NULL.'),

('data_staging', 'carrier_group_lookup', 'match_order',
 'Priority order for pattern matching (lower = higher priority)',
 'When multiple search terms match the same asset_id, the one with the lowest match_order wins (via DISTINCT ON ... ORDER BY match_order).',
 NULL,
 'INT, NOT NULL. Range: 1-10. Verizon terms are 1-3, AT&T/DISH are 4-5, TMO/USCC are 6-10.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    example_values = EXCLUDED.example_values,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();


-- ============================================================
-- SECTION D: Fix & enrich weak existing metadata
-- ============================================================

-- D1. FIX: stg_qa_form.requirement_status — still shows Pass/Fail in staging table
--     (Migration 022 only fixed analytics.v_qa_forms, not data_staging.stg_qa_form)
UPDATE agent.schema_metadata
SET description = 'QA requirement workflow status',
    business_context = 'Values: pending, submitted, approved, cancelled, in_progress. Use approved as "pass" and cancelled as "fail" for pass-rate calculations. User may say: "pass rate", "failure rate", "quality score", "requirement status".',
    example_values = ARRAY['pending', 'submitted', 'approved', 'cancelled', 'in_progress'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_qa_form' AND column_name = 'requirement_status';

-- D2. (past_due fix already handled in Section A4)

-- D3. NEW: stg_assets.carrier_group — missing since migration 026
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, example_values, data_notes, related_tables)
VALUES
('data_staging', 'stg_assets', 'carrier_group',
 'Carrier group for this asset (Verizon, AT&T/DISH, or TMO/USCC)',
 'Derived from asset_id pattern matching against carrier_group_lookup. Used for filtering and grouping by carrier. User may say: "carrier", "Verizon sites", "AT&T sites", "which carrier".',
 ARRAY['Verizon', 'AT&T/DISH', 'TMO/USCC'],
 'Can be NULL if asset_id does not match any search term in carrier_group_lookup. Backfilled by pipeline after asset extraction.',
 ARRAY['carrier_group_lookup (source of mapping via asset_id ILIKE search_term)'])
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    example_values = EXCLUDED.example_values,
    data_notes = EXCLUDED.data_notes,
    related_tables = EXCLUDED.related_tables,
    updated_at = NOW();

-- D4. NEW: analytics.v_asset_tasks.carrier_group — missing since migration 027
INSERT INTO agent.schema_metadata (schema_name, table_name, column_name, description, business_context, example_values, data_notes)
VALUES
('analytics', 'v_asset_tasks', 'carrier_group',
 'Carrier group for this asset (Verizon, AT&T/DISH, or TMO/USCC)',
 'Joined from stg_assets.carrier_group. Use for filtering tasks by carrier. User may say: "carrier", "Verizon tasks", "AT&T tasks", "show me by carrier".',
 ARRAY['Verizon', 'AT&T/DISH', 'TMO/USCC'],
 'Can be NULL if the asset has no carrier_group assigned.')
ON CONFLICT (schema_name, table_name, column_name) DO UPDATE
SET description = EXCLUDED.description,
    business_context = EXCLUDED.business_context,
    example_values = EXCLUDED.example_values,
    data_notes = EXCLUDED.data_notes,
    updated_at = NOW();

-- D5. FIX: stg_timer_activities table-level row count (~11.6K → ~273K)
UPDATE agent.schema_metadata
SET business_context = 'Incremental load - new data appends. ~273K entries. Use for labor analysis. User may say: "time logs", "timesheets", "work hours", "labor hours", "clock-in/clock-out", "time tracking", "hours worked", "time entries".',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name IS NULL;

-- D6. stg_user_priorities.status — add example_values
UPDATE agent.schema_metadata
SET example_values = ARRAY['pending', 'in_progress', 'submitted', 'approved', 'rejected', 'cancelled'],
    business_context = 'Current workflow status of this priority item. Values mirror task_status in stg_asset_tasks. User may say: "status", "state", "task status".',
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_user_priorities' AND column_name = 'status';

-- D7. stg_projects.status — add example_values
UPDATE agent.schema_metadata
SET example_values = ARRAY['in_progress', 'complete', 'pending'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_projects' AND column_name = 'status';

-- ============================================================
-- D8-D18. FK related_tables enrichments (11 column-level UPDATEs)
-- ============================================================

-- stg_asset_tasks: project_did → stg_projects
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_projects (via project_did = project_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_asset_tasks' AND column_name = 'project_did';

-- stg_asset_tasks: asset_did → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via asset_did = asset_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_asset_tasks' AND column_name = 'asset_did';

-- stg_qa_form: asset_did → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via asset_did = asset_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_qa_form' AND column_name = 'asset_did';

-- stg_qa_form: site_id → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_qa_form' AND column_name = 'site_id';

-- stg_timer_activities: asset_did → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via asset_did = asset_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name = 'asset_did';

-- stg_timer_activities: project_did → stg_projects
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_projects (via project_did = project_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name = 'project_did';

-- stg_timer_activities: site_id → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via site_id = asset_id)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_timer_activities' AND column_name = 'site_id';

-- stg_user_priorities: task_did → stg_asset_tasks
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_asset_tasks (via task_did = task_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_user_priorities' AND column_name = 'task_did';

-- stg_user_priorities: asset_did → stg_assets
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_assets (via asset_did = asset_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_user_priorities' AND column_name = 'asset_did';

-- stg_user_priorities: org_did → stg_organizations
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_organizations (via org_did = org_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_user_priorities' AND column_name = 'org_did';

-- stg_user_priorities: project_did → stg_projects
UPDATE agent.schema_metadata
SET related_tables = ARRAY['stg_projects (via project_did = project_did)'],
    updated_at = NOW()
WHERE schema_name = 'data_staging' AND table_name = 'stg_user_priorities' AND column_name = 'project_did';
