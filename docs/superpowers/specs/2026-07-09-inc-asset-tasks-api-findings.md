# Swift API lastUpdated Semantics: Findings for the Incremental Asset-Tasks Pipeline

Source: Jamil's manual verification in browser devtools against the live Swift API, 2026-07-09. This doc is the authority for the incremental walker's pruning rules (see `docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md`).

## Reliability table (verified empirically)

| Level / collection | Check this key | Reliable? | Notes |
|---|---|---|---|
| Organization | `lastUpdated` | NO, never moves | Frozen regardless of activity underneath. Never check it; start at the project list. |
| Project | `lastUpdated` (top-level) | YES | Matches `metrics.lastUpdated` in every observed case. Best single signal for "did anything in this project move." |
| Asset-Project | `lastUpdated` (top-level) | YES | Matches `metrics.lastUpdated`. A bump means some asset-task under it moved. |
| Asset-Task | `lastUpdated` (top-level) | YES, key layer | Reliably reflects submits, approvals, cancellations, AND file-requirement changes (uploads/removals), even when the requirement doc itself does not update. |
| Asset-File-Requirement | `lastUpdated` / `metrics.lastUpdated` | NO, unreliable | File upload/removal changes the count fields (`fileUploadedCount`, `fileSubmittedCount`, `status`) but does NOT bump either lastUpdated. Check the counts directly, or rely on the parent asset-task's bump. |
| Task template (not asset-specific) | `lastUpdated` | YES, but rarely changes | Only moves when the template itself is edited (usually one-time setup). |
| Personnel | `lastUpdated` | YES, but wrong signal | Reflects changes to the person record (profile edits, lastSeen/lastOnline). NOT project activity; never use for pruning. |

## Decision flow the walker implements

1. Org level: skip entirely (frozen). Start from the project list.
2. `project.lastUpdated` unchanged since watermark: stop, nothing anywhere in the project moved.
3. Changed: fetch asset-projects, compare each `lastUpdated` against stored; descend only into movers.
4. For each moved asset-project: fetch asset-tasks, compare `lastUpdated`, write only movers.
5. Never gate anything on a file-requirement's own lastUpdated. If requirement detail is needed, fetch it because the PARENT asset-task bumped, and read `metrics.fileUploadedCount` / `fileSubmittedCount` / `status` for file state.

One-line rule: trust `lastUpdated` at every level except asset-file-requirements; there, check the count fields instead.

## Probe results (Task 1, verified live 2026-07-09)

Probed with `swift_api_pipeline/probe_inc_asset_tasks.py` against TECH-OPS: TS17
(5,025 asset rows) and TECH-OPS: TS19 (4,727 asset rows). Both projects returned
identical shapes; per-key row counts below are TS17's (TS19 differed only in totals).
Key names only; no payload values recorded (rows contain names/emails).

### (a) Payload key inventories per endpoint

**Project row** (`GET /api/organizations/{org_id}/projects`), all keys present:
`ETag, collection, createdBy, dateCreated, description, id, isPrivate, lastUpdated, locationOrientation, metrics, name, organization, status, validStatuses`

**Asset-project row** (`GET /api/projects/{project_did}/assets`), 5,025 rows:
- On every row: `id, org, name, asset, status, metrics, project, createdBy, shortName, collection, dateCreated, lastUpdated, validStatuses, ETag`
- Sparse: `identifier` (5,022), `completedBy`/`completedOn` (163), `cancelledBy`/`cancelledOn` (86)

**Asset-task row** (`GET /api/asset-projects/{id}/asset-tasks`), 89 rows for the sample asset. The listing MIXES collections; filter on `collection == "asset-tasks"` (the daily-reports pipeline's existing `collection == "milestones"` skip is confirmed necessary):
- On every row (any collection): `id, name, collection, ETag`
- On real asset-tasks (78 rows): `org, project, createdBy, dateCreated, lastUpdated, metrics, ast, task, status, useTime, isPrivate, milestone, assetProject, validStatuses, isPinned, calStat` (plus `description` on most)
- Milestone rows (10) carry `assetSpecific` + `metrics.taskCount` instead; 1 anomalous row (TS17 sample only) carried `scheduledBy` and NO `lastUpdated`/`metrics`/`org`/`project`. Walker must tolerate rows missing `lastUpdated`/`metrics`.

### (b) FK for the asset-tasks endpoint: CONFIRMED `id`

The asset-project row's top-level `id` (NOT `asset.id`) is what
`/api/asset-projects/{id}/asset-tasks` takes; the probe fetched tasks with it
successfully on both projects, exactly as `extract_daily_reports.py` does.

### (c) id uniqueness: PASS (with a milestone caveat)

- Asset-project `id`: unique within project. TS17 5,025/5,025; TS19 4,727/4,727.
- Asset-task `id`: unique within an asset (89/89, 87/87) AND across assets. Swept the first 40 assets per project (capped at 40 to keep the one-off probe fast): TS17 3,121/3,121 unique, TS19 3,040/3,040 unique for `collection == "asset-tasks"` rows.
- CAVEAT: the RAW listing is NOT unique across assets, because the ~11 project-level `milestones` rows repeat under every asset (TS17: 3,561 rows -> 3,132 unique; all 429 duplicates were milestone rows). After the milestone filter, `id` is a safe natural key for upserts.

### (d) Requirement-count field: `metrics.reqCount`

- Asset-project level: `metrics.reqCount` (with per-status splits `reqPending/reqApproved/reqRejected/reqCancelled/reqSubmitted/reqInProgress`).
- Asset-task level: same `metrics.reqCount` + splits (what the daily-reports pipeline already reads).

### (e) Metrics child-count fields (GC-scale deletion strategy): PRESENT at every level

- **Asset-project `metrics`** (on 100% of rows): `taskCount` + per-status `taskPending/taskApproved/taskRejected/taskCancelled/taskSubmitted/taskInProgress/taskHasRejection`, `milestoneCount`, `reqCount` + splits, and file/form requirement counters (`fileRequirementCurrent/Max/Min/Approved/Rejected/Submitted`, `formRequirementTotal/Current/Approved/Rejected`). A stored-vs-fetched `taskCount` mismatch detects task deletion under an asset WITHOUT descending -> count-based reconcile is viable at GC scale.
- **Project `metrics`**: counts live in NESTED dicts, not top-level. `metrics.asset` aggregates across all asset-projects: `assetProjectCount` (TS19's matched the fetched row count exactly, 4,727; TS17 reported 5,062 vs 5,025 fetched, a +37 drift; treat project-level aggregates as advisory, not reconciliation-grade), plus `taskCount`, `reqCount`, `milestoneCount` and the same per-status/file/form splits. `metrics.project` carries the same shape for project-level (non-asset) tasks only. `metrics.lastUpdated` and `metrics.status` are the other members.
- **Asset-task `metrics`**: `reqCount` + splits (see d); requirement deletion under a task is likewise count-detectable.

### Still open: DELETE propagation (pending-human-test)

Does hard-deleting an asset-task bump the parent asset-project/project
`lastUpdated`? Submits/approvals/cancellations are confirmed to propagate; hard
deletes remain UNTESTED (requires a human to delete a task in Swift; not
scriptable read-only). **Until confirmed, the weekly `--full-walk` safety net
stays mandatory** (Task 6 wires it behind a flag either way).

Test procedure (Jamil): pick one disposable test task in a pilot project; run
`python probe_inc_asset_tasks.py --project-did=<did> --project-name "<name>"`
and note the owning asset's `lastUpdated` (or query it directly); delete the
task in Swift; re-run and compare the asset-project's and project's
`lastUpdated` before/after. If they bump, count-based reconcile + lastUpdated
pruning fully covers deletes and the weekly full-walk can be demoted to a
paranoia check. Note: the asset's `metrics.taskCount` drop is an independent
delete signal even if `lastUpdated` does NOT bump.
