# Timer → Revenue: Market Crosswalk Design

**Status**: Design (verified against live DB 2026-08-02). Not yet implemented — no
migration, no pipeline change.
**Context**: The timer→revenue feasibility (2026-07-31) found task-name joins to
`reference.ref_task_revenue_rates` work (68.3%) but timer data had no fine market.
The RevMetrics workbook (`local-pipeline/reference/revenue-metrics/README.md`) showed
the fine market is derived from the Swift **asset path**. This spec ports that to the DB.

## Verified facts (live queries, 2026-08-02)

1. **`data_raw.raw_assets.asset_identifier` IS the Excel "Site ID" path.**
   Format: `GC[/Sub]/Carrier/Market/Scope/ProjectNumber/Date`, e.g.
   `CNB/New-Tech Construction/VZW/FL/Embedded/16285600/Dec 2024`.
   35,292 of 35,302 rows (99.97%) have it; 33,419 distinct `asset_did`.
2. **Join coverage is total.** Of 392,366 `stg_timer_activities` rows, 281,888 have an
   `asset_did`; **281,886 (100.0%) match a raw asset with a path**. The other 28.2%
   of timer rows have no asset link = admin/overhead (Excel also excludes these).
3. **Segment count varies 1–13** (6 and 7 most common) — parsing must anchor on the
   carrier token (VZW, AT&T, TMO, DISH, FTTH, …), not fixed positions. Signature =
   carrier segment + next two (`VZW/BAWA/Embedded`, `AT&T/OH - Overlay/5G`).
4. **Ported col-67 rules classify 98.9% of asset-linked timer rows** into the 14 rate
   buckets (all buckets populate; VZW Embedded/Macro dominates with 187.8k rows).
   The 1.1% residue (3,212 rows / 741 assets) is almost entirely **2023 legacy TSC
   work** for regional carriers (Viaero, Nemont) not in the current rate sheet, plus
   crumbs (`NIS/GA-AL/...` paths missing the carrier prefix, `NB+C SE/Motorola`).
5. **`data_staging.stg_assets` does NOT carry the path** (only the 3-bucket
   `carrier_group`). Staging dropped it; it must be re-surfaced.

## Design

### 1. Surface the path in staging
Add `asset_identifier text` to `data_staging.stg_assets` and populate it in the asset
upsert (it's already in the raw payload/landing table — no new API calls). Backfill
existing rows from `data_raw.raw_assets` (`DISTINCT ON (asset_did) … ORDER BY
loaded_at DESC`). Until then, prototypes can join raw directly.

### 2. Crosswalk table: `reference.ref_market_bucket_crosswalk`
One row per **distinct market signature**, not a regex function — auditable, and
manual patches (Excel needed `TSC/Jul 2026 → TMO`) become plain rows.

```
market_signature  text PRIMARY KEY   -- e.g. 'VZW/BAWA/Embedded' (carrier + 2 segments)
market_bucket     text NOT NULL      -- one of the 14 ref_task_revenue_rates buckets,
                                     -- or 'EXCLUDED' (admin/legacy/non-billable)
source            text NOT NULL      -- 'rule' | 'manual'
notes             text
created_at / updated_at
```

Seed: extract all distinct signatures from `raw_assets`, apply the ported col-67
rules (below), leave residue as `EXCLUDED` rows with notes. New unmapped signatures
surface via a QA query (or pipeline warning) rather than silently pricing at 0.

### 3. Signature extraction (SQL, port of Excel `cCarrierMarket`)
Anchor: first path segment matching a carrier token
(`VZW|AT&T|TMO|T-Mobile|DISH|FTTH|USCC|US Cellular|Westell|Gulf Services|AAHI`),
then take that segment + the following two. FTTH caveat: third segment is a date
(`FTTH/Phase 1/Mar 2026`) — signature for FTTH should be first two segments only.

### 4. Bucket rules (ported from Excel col 67 `Market/Project`, verified above)
- VZW + (Embedded|Macro|NSB), not Small Cell/Decom/AAHI/Ground Scope → `VZW Embedded / Macro`
- VZW + Small Cell → `VZW Small Cell`
- DONOR → `AAHI/DONOR`; MDU → `AAHI/MDU`
- Ground Scope | Shelter Remodel | BidWalk → `Ground Scope`
- Decom → `VZW Decom`; Westell → `Westell/CGC`
- AT&T → `AT&T`; DISH → `Dish Wireless`; TMO → `TMO`
- FTTH + (Phase 1|Backbone) → `FTTH Phase 1`; FTTH + Phase 2 → `FTTH Phase 2`
- US Cellular|USCC → `USCC`; Gulf Services → `Gulf Services`
- Manual rows: `TSC/Jul 2026`-style Site IDs → `TMO` (Excel patch); 2023 TSC/Viaero/
  Nemont legacy → `EXCLUDED`.

### 5. Revenue attribution (phase 2, from Excel col 69 `Amount`)
Price per **(asset, task) completion**, split across techs by share of cleaned timer
minutes; duplicates/admin/zero-duration excluded. Final COP / Live Review bundling
depends on an **LR-approval signal** (Excel: `Snapshot_LR` tab) — DB equivalent not
yet identified. Open item before phase 2.

## Open items
- LR-approval signal source in our DB (for FCOP/LR bundling).
- Where the serving view lives (`analytics.v_…`) and what grain ontel-people needs.
- Refresh/QA: new signatures alert; crosswalk is `reference` schema → migration +
  RLS deny-all per DATABASE_ARCHITECTURE.md.
- Rate sheet churn: `ref_task_revenue_rates` refreshes must not orphan bucket names
  (crosswalk buckets FK-able to a distinct list if desired).
