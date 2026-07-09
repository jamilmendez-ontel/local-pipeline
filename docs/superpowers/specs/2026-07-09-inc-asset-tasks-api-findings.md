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

## Still open (Task 1 of the plan verifies these)

- Exact payload key inventories per endpoint for a TS project (field names for the FIELD_MAP constants; the daily-reports pipeline's shapes are the expected default).
- The asset row field used as the FK by `/api/asset-projects/{id}/asset-tasks` (expected `id`).
- `id` uniqueness within project for assets and tasks (natural-key requirement for the upserts).
- DELETE propagation: does deleting an asset-task bump the parent asset-project/project `lastUpdated`? Submits/approvals/cancellations are confirmed to propagate; hard deletes were not tested. Until confirmed, the weekly `--full-walk` safety net stays mandatory.
