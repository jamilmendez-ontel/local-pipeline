# Swift `_export` data loss — root cause (2026-07-13)

**Status:** root cause proven from landed data; no code changes required to reproduce.
**Scope:** current (production) asset-tasks pipeline, all projects. Verified on the three
shadow-pilot projects (TS17 `-ONLJdAstPfeGwVNgpYH`, TS18 `-O_IpQNpLVwhdVC3QYIm`,
TS19 `-OmzvGwfYsSskngv6SEo`) where the incremental shadow provides ground truth.

## TL;DR

`GET /api/next/projects/{did}/assets/_export` does **not** export the project's task
*instances*. It exports the project's **task-template matrix** — every *template* task
name × every asset — and nothing else. Any task added **ad hoc to a single asset**
(in practice: repeat revision/rejection rounds like "15B. COP Rejections Reviewed **2**",
"…Reviewed **3**", "4B. LL COP Revision Complete **2**") is structurally invisible to the
export. This is not a pagination bug, not a filter we control, and not our transform:
the rows never arrive in `data_raw`.

## Evidence

1. **Exact factorization of the export.** In `data_staging.stg_asset_tasks` (current):
   - TS18: 78 distinct task names × 5,481 assets = **427,518 = rows_current exactly**
   - TS19: 76 distinct task names × 4,847 assets = **368,372 = rows_current exactly**
   - TS17: 79 × 5,027 = 397,133 vs 392,104 actual (template evolved over time — not all
     names apply to all assets — but the class boundary below still holds exactly)
   Every exported task name in TS19 has exactly 4,847 instances — one per asset.

2. **100% class exclusion, not row-level drops.** For every one of the ~40 distinct
   dropped task names, current has **zero** instances project-wide while the shadow has
   all of them (`n_dropped = n_in_shadow`, `n_in_current = 0` for every name).

3. **Not our transform.** All 54 (TS19) and 142 (TS17) dropped task DIDs are absent from
   the raw landed payloads (`data_raw.raw_asset_tasks_ts*`) — the API never returned them.

4. **Not pagination.** Only 6 of 40 dropped-task assets in TS19 straddle a 1000-row page
   seam (base rate 7.5% — no enrichment). Hypothesis tested and refuted.

5. **Shadow is truth.** The dropped tasks were spot-verified live in Swift on 2026-07-12,
   and the hourly shadow reconciliation keeps re-confirming them.

This also explains the second known defect (ghost rows): an asset removed from the
project hierarchy stays in the export's asset set, so its template rows keep being
emitted (TS17 asset `-OOgqCUgxootyP0k2sVU`, 77+ tasks nightly) even though the live
hierarchy no longer contains it.

## Impact

- 295 real Swift tasks are missing from `data_staging.stg_asset_tasks` across the 3 pilot
  projects alone (279 approved; approvals span 2025-05 → 2026-07).
- **146 of them match the billable filter of `analytics.v_weekly_invoice_billable_tasks`**
  (`approved` + `(COP|Revision).*Complete`), 68 approved in 2026 — i.e. approved,
  billable revision rounds that never appeared on the weekly invoice worklist.
- 16 downstream objects read `stg_asset_tasks` (incl. `mv_daily_completion`,
  `mv_technician_stats`, scorecard views) — all undercount repeat revision work.
- The defect is structural and affects **every** project loaded via `_export`, not just
  the pilot three; only the pilot has shadow data to measure it.

Full row-level list: `out/export_dropped_tasks_2026-07-13.csv` (295 rows: project_did,
asset_id/name, task_did/name/status, scheduled/submitted/approved dates, approver,
`billable_pattern` flag; local-only, not committed).

## Consequences / recommendations (Jamil's call)

1. **Revenue recovery (urgent, manual):** review the 146 billable-pattern rows in the CSV
   against actual invoices; anything not caught manually was never billed.
2. **Audit doctrine (unblocked):** the strict gate can never go green against
   `stg_asset_tasks` — the baseline is provably lossy. Flip the audit to treat the
   hierarchy walk as truth: assert `current ⊆ shadow` plus the two documented defect
   classes (ad-hoc tasks shadow-only; ghost-asset rows current-only), instead of hash
   equality. No spot-fetch infrastructure needed for this: the class rule explains 100%
   of the remaining row-level drift.
3. **Cutover weight:** the shadow pipeline is strictly more accurate than production on
   row coverage; this finding is an argument for accelerating phase gates rather than
   waiting for a "clean audit week" that is mathematically impossible against current.
4. **Vendor:** report the `_export` template-matrix limitation to Swift (ad-hoc task
   instances + deleted-asset rows) — it silently affects any consumer of that endpoint.
