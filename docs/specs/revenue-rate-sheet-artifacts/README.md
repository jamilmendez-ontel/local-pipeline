# Revenue Rate Sheet — artifacts

Source artifacts for `reference.ref_task_revenue_rates` (OntelDB, project `voqfjfngdpcvevbkikud`).
The rate sheet maps every billable task type, per market bucket, to a stated duration and a
revenue amount. It is the lookup joined against `data_staging.stg_asset_tasks` (strip the numeric
prefix via `task_name_norm`) to attribute revenue to completed tasks.

The `.xlsx` / `.csv` sources are gitignored (binary, kept locally). This README plus the
committed migration SQL are the portable, version-controlled record.

## Table shape — `reference.ref_task_revenue_rates`
| column | type | notes |
|---|---|---|
| `id` | `bigint` | PK, `GENERATED ALWAYS AS IDENTITY` — never supplied on insert |
| `market_bucket` | `text NOT NULL` | one of 15 buckets (see below) |
| `task_name` | `text NOT NULL` | task label as exported |
| `task_name_norm` | `text NOT NULL` | task_name with any leading ordering prefix stripped (equals task_name for current data) |
| `duration_hrs` | `numeric(5,2)` | **nullable as of 2026-07-31** — NULL = duration not stated (distinct from an intentional 0) |
| `amount_usd` | `numeric(10,2) NOT NULL` | revenue for the task |
| `source_file` | `text` | originating export filename |
| `created_at` / `updated_at` | `timestamptz` | default `now()` |

Constraints: `PRIMARY KEY (id)`, `UNIQUE (market_bucket, task_name)` (the upsert key).
Dependent: `reference.vw_task_revenue_rates_with_red_avg` (plain view).

## Market buckets (15)
VZW Embedded / Macro, VZW Small Cell, AAHI/DONOR, AAHI/MDU, Ground Scope, VZW Decom,
Westell/CGC, AT&T, Dish Wireless, TMO, FTTH Phase 1, FTTH Phase 2, USCC, Gulf Services,
Viaero (added 2026-08-27; same rate card as VZW Small Cell, not in the xlsx export).

## Load history
| date | source file | rows | migration |
|---|---|---|---|
| 2026-05-12 | `Revenue Metrics_Duration & Amount (1).xlsx` | 239 | `migration_revenue_rates.sql` (initial create + full load) |
| 2026-07-31 | `Revenue Metrics_Duration & Amount_20260731 (1).xlsx` (`revenue_metrics_20260731.xlsx`) | 242 | `migration_revenue_rates_update_20260731.sql` (upsert diff) |
| 2026-08-27 | (no file; Jamil: Viaero = VZW Small Cell rate card) | 271 | `migration_revenue_rates_viaero_20260827.sql` = migration `247` (29-row clone) |

### 2026-07-31 update — diff vs prior (3 added, 2 changed, 0 removed)
**Added**
- Ground Scope · Live Review Complete → 1 hr / $40
- VZW Embedded / Macro · 360 Tour Complete → (no duration) / $100
- VZW Small Cell · 360 Tour Complete → (no duration) / $100

**Changed**
- Ground Scope · Data Pre-Fill Complete: 0.2 hr/$28 → 0 hr/$0
- Ground Scope · Final COP Complete: 1 hr/$100 → 1 hr/$128

The two "360 Tour Complete" rows have a real amount but a blank Duration Hrs in the sheet, so
`duration_hrs` was made nullable and those rows store NULL. There are also ~48 rows with an
intentional `0` duration + `0` amount (unbilled revision steps) — these are 0, not NULL.

### 2026-08-27 update — Viaero bucket (29 added, 0 changed, 0 removed)
Viaero bills on the VZW Small Cell rate card. The 29 `VZW Small Cell` rows were cloned
row-for-row (task, duration, amount) under `market_bucket = 'Viaero'`, `source_file =
'clone of VZW Small Cell rates (2026-08-27)'`. Keep the two buckets in sync on future
rate-sheet refreshes: the xlsx export has no Viaero rows, so a diff script must apply
any VZW Small Cell change to Viaero as well.

Crosswalk wired the same day by migration `248_market_crosswalk_viaero.sql`: `Viaero` added
to the `reference.market_signature()` anchor regex and to the `seed_new_market_signatures()`
rules; 5 signatures seeded (`Viaero/CO-NE - Overlay/LTE` etc.), 309/309 Viaero assets classify,
`analytics.mv_timer_revenue` refreshed.

## Files
- `revenue_metrics_20260731.xlsx` — latest export (gitignored). Columns: `Market/Project`, `Task`, `Duration Hrs`, `Amount`. 242 data rows.
- `migration_revenue_rates.sql` — original create + 239-row load (2026-05-12).
- `migration_revenue_rates_update_20260731.sql` — the 2026-07-31 upsert diff (applied via Supabase MCP).
- `migration_revenue_rates_viaero_20260827.sql` — the 2026-08-27 Viaero clone (= `swift_api_pipeline/migrations/247_ref_task_revenue_rates_viaero.sql`).
- `generate_migration.py` — helper that generated the original full-load INSERT from the xlsx.
