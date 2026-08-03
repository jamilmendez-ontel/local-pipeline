# Revenue Metrics reference — RawTimeData_Combined_RevMetrics_20260728.xlsx

**File**: `RawTimeData_Combined_RevMetrics_20260728.xlsx` (~698 MB, gitignored, kept locally
in this folder; original in `~/Downloads`).
**Source**: Jamil, 2026-08-02. RevMetrics variant of the master RawTimeData_Combined
workbook (SharePoint Data Repository assembly of monthly TimeData_TS* exports).
**Why it matters**: it contains the working Excel implementation of **timer entry →
fine market bucket → revenue amount**, which is exactly the blocker the 2026-07-31
timer→revenue feasibility session hit (timer data has no fine market; DB only has the
3-way `carrier_group`).

## Differences vs the documented base RawTimeData file (39 tabs, 66 cols)

- **New tab `Revenue Metrics System`** (242 data rows): the rate sheet itself.
  Columns: `Market/Project`, `Task`, `Duration Hrs`, `Amount`, `Concat` (lookup key
  = market & task concatenated). Row-for-row equivalent of
  `reference.ref_task_revenue_rates` (same 242 rows / 14 market buckets; this file is
  dated 2026-07-28, DB refreshed 2026-07-31 from the same series).
- **RawTimeData grew 66 → 69 columns.** The three new computed columns are the
  revenue engine:
  - **Col 67 `Market/Project`** — maps `cProject_ACTUAL` (fine market like
    `VZW/MP/Macro`) to the 14 rate-sheet buckets via SEARCH rules:
    - VZW + (Embedded|Macro|NSB) → `VZW Embedded / Macro`
    - VZW + Small Cell → `VZW Small Cell`
    - `AAHI/DONOR`, `AAHI/MDU` verbatim
    - Ground Scope OR `Available Power/Shelter Remodel` → `Ground Scope`
    - `VZW/Decom` → `VZW Decom`; `Westell/CGC` → `Westell/CGC`
    - AT&T → `AT&T`; DISH → `Dish Wireless`; TMO → `TMO`
    - FTTH + (Phase 1|Backbone) → `FTTH Phase 1`; FTTH + Phase 2 → `FTTH Phase 2`
    - US Cellular → `USCC`; Gulf Services → `Gulf Services`
    - Site ID `TSC/Jul 2026` or `STS Communication/Jul 2026` → `TMO` (manual patch)
    - `Administrative Tasks` → excluded; else `#N/A`
  - **Col 68 `Market/Project_cTask`** — concat key into `Revenue Metrics System`.
  - **Col 69 `Amount`** — revenue attribution per timer row:
    `rate(market, task) × (tech's minutes on site+task ÷ total minutes on site+task)`,
    zeroed when the row is a duplicate (`cDupCheck=0`), zeroed duration, or admin task.
    Special Final COP / Live Review bundling driven by `cLRStatus` (Snapshot_LR
    approval): LR **not** approved → Final COP row earns FCOP+LR amounts and the LR
    row earns 0; LR approved → each earns its own rate.

## The market-derivation chain (answers feasibility "direction A")

`Site ID` (the Swift asset path: `GC/Carrier/Market/Scope/ProjectNumber/Date`)
→ `cCarrierMarket` (parsed path segments; blank Site ID → Administrative Tasks)
→ `cProject_ACTUAL` (refined w/ date + site overrides)
→ `Market/Project` (col-67 rules above)
→ rate lookup.

So the fine market **is derivable from the asset path** — the same path exists in our
Swift raw asset data. The DB port needs: asset path → `cProject_ACTUAL`-equivalent →
col-67 bucket rules (a `reference` crosswalk table, not a formula).

## Grain / overcounting solution (answers feasibility "direction B")

Revenue is priced per **(site, task) completion**, then split across techs
proportionally by their share of cleaned timer minutes on that site+task. Duplicate
timer rows are zeroed first (cDupTimer/cDupCheck logic documented in the base-file
README, see memory `reference_rawtimedata_file`). Note: `Amount` is populated on every
timer row, so summing it naively still overcounts a tech with multiple rows on one
site+task — downstream pivots (Pts Breakdown / Revenue Metrics tab) aggregate at the
site+task+tech grain.

## Other tabs of interest

- **`Revenue Metrics`** (1,478 rows): per `cProject_ACTUAL` × task —
  SiteCountRaw/Used, Avg/Median/Fastest hours, TotalTaskHours, TotalProjectHours.
  This is the report layer over the engine.
- **`Classification`** (~136 rows): fine market (`VZW/FL/Embedded`) → Cluster,
  Scope-of-Work, Site Setup Process. Candidate seed for the crosswalk table.
- Everything else matches the base-file documentation in memory
  (`reference_rawtimedata_file.md`): RawTimeData 82.5k rows here, OOT Timer 96.8k,
  points matrices, TimeDiscrepancies, Manual Entry, etc.
