# Revenue Rate Sheet Reference Table — Design

**Date**: 2026-05-12
**Status**: Approved (design phase)
**Source file**: `C:\Users\admin\Downloads\Revenue Metrics_Duration & Amount (1).xlsx`

## Purpose

Load the Revenue Metrics rate sheet (239 rows: market_bucket × task → duration_hrs, amount_usd) into Supabase as a reference table. Two downstream consumers:

1. **Revenue analytics** — joined against `data_staging.stg_asset_tasks` / `analytics.v_asset_tasks` to compute revenue and labor hours per market, technician, or date.
2. **DARA / AI agent** — agent looks up "how much does task X pay in market Y?" and "expected duration for Z?" via the same table.

## Non-Goals

- **No Market/Project → project_did mapping.** The 14 xlsx market buckets do not match `stg_projects.project_name` 1:1; that mapping is deferred until the revenue dashboard is built. The table stores `market_bucket` as plain text.
- **No effective-dating / rate history.** Rates change rarely; updates happen in-place. If history is needed later, add a separate `_history` table.
- **No Google Sheets sync.** Maintenance is manual SQL updates.
- **No FK to `stg_projects` or `stg_asset_tasks`.** Joins are done by normalized task name + market bucket text.

## Storage

**Schema**: `reference` (already used for `ref_employees`, `ref_nni_directory`, `ref_ontel_techops_projects`).

**Table**: `reference.ref_task_revenue_rates`

```sql
CREATE TABLE reference.ref_task_revenue_rates (
  id              bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  market_bucket   text NOT NULL,
  task_name       text NOT NULL,
  task_name_norm  text NOT NULL,
  duration_hrs    numeric(5,2) NOT NULL,
  amount_usd      numeric(10,2) NOT NULL,
  source_file     text,
  created_at      timestamptz DEFAULT now(),
  updated_at      timestamptz DEFAULT now(),
  UNIQUE (market_bucket, task_name)
);

CREATE INDEX idx_revenue_rates_market    ON reference.ref_task_revenue_rates(market_bucket);
CREATE INDEX idx_revenue_rates_task_norm ON reference.ref_task_revenue_rates(task_name_norm);
```

## Column Reference

| Column           | Notes                                                                                 |
|------------------|---------------------------------------------------------------------------------------|
| `id`             | Surrogate PK, auto-generated.                                                         |
| `market_bucket`  | One of 14 xlsx values (e.g., `"VZW Embedded / Macro"`, `"AT&T"`, `"TMO"`).            |
| `task_name`      | Raw task name as it appears in the xlsx (may include `"N. "` prefix or `" 2"` suffix).|
| `task_name_norm` | `regexp_replace(task_name, '^\d+\.\s+', '')` — precomputed for fast joins.            |
| `duration_hrs`   | `numeric(5,2)` — billed hours per task.                                               |
| `amount_usd`     | `numeric(10,2)` — billed USD per task.                                                |
| `source_file`    | Provenance — set to `"Revenue Metrics_Duration & Amount (1).xlsx"` on initial load.   |
| `created_at`     | Row creation timestamp.                                                               |
| `updated_at`     | Row update timestamp (manual or via trigger; not auto-updated initially).             |

**Uniqueness**: `(market_bucket, task_name)` — not `task_name_norm` — so two raw variants of the same normalized task could coexist in the same market (rare but preserved).

## The 14 Market Buckets

VZW Embedded / Macro, VZW Small Cell, AAHI/DONOR, AAHI/MDU, Ground Scope, VZW Decom, Westell/CGC, AT&T, Dish Wireless, TMO, FTTH Phase 1, FTTH Phase 2, USCC, Gulf Services.

## Data Transformations on Load

1. Strip leading/trailing whitespace from `market_bucket` and `task_name`.
2. Compute `task_name_norm = regexp_replace(task_name, '^\d+\.\s+', '')`.
3. **Keep zero rows** — rows with `duration_hrs = 0 AND amount_usd = 0` are preserved. They signal "this revision step exists in the workflow but is unbilled in this market," and protect against silent dropping if rates change later.
4. Set `source_file = 'Revenue Metrics_Duration & Amount (1).xlsx'`.

## Load Strategy

**One-shot SQL migration.** A migration file generates the `CREATE TABLE` + `INSERT` statements for all 239 rows from the xlsx. Applied via `apply_migration`.

- Migration name: `add_ref_task_revenue_rates`
- The INSERTs are generated locally from the xlsx (Python helper script that reads the workbook and emits SQL), then pasted into the migration body. The helper is not retained — this is a one-time operation.
- Future edits: a new migration if the xlsx is re-issued (preferred — keeps history). Ad-hoc rate tweaks via direct `UPDATE` are acceptable but should be noted in a comment.

## Downstream Join Pattern

When the revenue dashboard is built:

```sql
SELECT
  at.project_did,
  at.task_name,
  rr.duration_hrs,
  rr.amount_usd
FROM data_staging.stg_asset_tasks at
JOIN reference.ref_task_revenue_rates rr
  ON regexp_replace(at.task_name, '^\d+\.\s+', '') = rr.task_name_norm
 AND rr.market_bucket = <bucket-derived-from-project>;
```

The `<bucket-derived-from-project>` piece is the deferred mapping. Options when we build the dashboard:

- A mapping table `reference.ref_market_bucket_mapping(project_did, market_bucket)`.
- A `market_bucket` column on `stg_projects`.
- Pattern rules in a view.

## DARA Integration

The `agent.schema_metadata` table powers DARA's awareness of available schemas. To make this table discoverable:

1. Confirm the schema_metadata scanner's current schema coverage and whether it includes `reference`. (Not verified during design.)
2. If not, extend the scanner to include `reference` OR insert a manual row into `agent.schema_metadata` describing the table.
3. Add a short description and a few example NL prompts so DARA's query planner has context (e.g., "how much do we make per COP revision in AT&T?").

This is a follow-up task, not part of the initial migration.

## Risks / Open Items

- **schema_metadata scanner coverage** for the `reference` schema is unverified — needs a check before DARA can use this table without manual help.
- **MEMORY.md previously flagged `reference` as off-limits** — corrected on 2026-05-12. Future agent sessions should now treat `reference` as ours.
- **Zero-row interpretation**: kept by decision. If future analytics queries need to exclude unbilled rows, filter with `WHERE amount_usd > 0`.
- **Market bucket text drift**: any rename in the xlsx breaks downstream joins. Mitigated by manual update and the small bucket count (14).

## Out of Scope (Future Work)

- Market/Project → project_did mapping table or column.
- DARA `schema_metadata` row for the table (after scanner coverage is verified).
- A revenue dashboard view in `analytics`.
- Effective-dating / rate history.
