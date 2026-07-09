# Incremental Asset-Tasks Shadow Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a duplicate asset-tasks pipeline for TECH-OPS TS13+ that walks the Swift project/asset/task hierarchy pruned by `lastUpdated`, fetching and writing only what changed, running in parallel with (and never touching) the current full-reload pipeline until a nightly drift audit proves byte-equality.

**Architecture:** First run seeds a full baseline. Every later run fetches the cheap asset list per project, descends only into assets whose `lastUpdated` moved past what's stored, fetches task lists only for those assets, and upserts only tasks whose `lastUpdated` moved. Deletions are handled by keep-list reconcile inside every scope that was actually visited with a successful fetch. The stored rows themselves are the watermark (self-healing); a per-project skip check against `pipeline.content_watermarks` avoids even the asset-list call for untouched projects. All DB access goes through the transaction-mode pooler (port 6543) so N workers hold zero idle session slots.

**Pruning rules are empirically verified** (Jamil, devtools, 2026-07-09; authority: `docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md`): org lastUpdated is frozen so the walk starts at the project list; project / asset-project / asset-task lastUpdated are all reliable, and the asset-task level even bumps for file-requirement upload/removal. The ONE exception: never gate on a file-requirement's own lastUpdated (it does not move); read its `metrics.fileUploadedCount` / `fileSubmittedCount` / `status` count fields instead, and only ever fetch requirements because the parent asset-task bumped.

**Tech Stack:** Python 3.12, asyncpg via a new `db_tx.py` (transaction pooler, `statement_cache_size=0`), requests, ThreadPoolExecutor (6 workers, same as current pipeline), GitHub Actions (workflow_dispatch only during pilot), pytest for pure-function tests.

## Global Constraints

- Schemas: new tables ONLY in `data_raw` / `data_staging` / `pipeline` per DATABASE_ARCHITECTURE.md; snake_case; every object via migration; RLS enabled on every new table; PK on anything upserted.
- Shadow naming: `_inc` suffix (`raw_asset_tasks_inc`, `stg_assets_inc`, `stg_asset_tasks_inc`). These are PERMANENT candidates, not temp tables, but the plan ends with an explicit teardown/cutover decision; never name anything `temp_*`/`adhoc_*`.
- The current pipeline (`extract_asset_tasks.py`, `raw_asset_tasks*`, `stg_asset_tasks`) is READ-ONLY for this project. No edits, no shared state except reading `reference.ref_ontel_techops_projects`.
- NO `run_id != current` sweep deletes anywhere. Keep-list reconcile only, and only inside a scope whose fetch succeeded (ok-flag pattern from `extract_daily_reports.py` Step 5).
- Upserts must be guarded: `ON CONFLICT ... DO UPDATE ... WHERE (...) IS DISTINCT FROM (...)` on payload columns. Never bump `loaded_at`/`run_id` on unchanged rows (Disk IO lesson 2026-07-09).
- DB via transaction pooler `aws-0-ap-southeast-1.pooler.supabase.com:6543`, `statement_cache_size=0`, no session-scoped features (no session SET, no advisory locks, no LISTEN/NOTIFY). Per-statement timeouts via `SET LOCAL` inside explicit transactions where needed.
- Timestamps from Swift are epoch millis; store as `timestamptz` (UTC). Display conversions to America/New_York only at reporting time.
- Migration numbering: LIST `swift_api_pipeline/migrations/` for the next free number before creating (176 expected next; 157/170 collisions happened before).
- Scope: projects from `reference.ref_ontel_techops_projects WHERE project_number >= 13` (same source the current pipeline uses).
- Commit after every task; push only when the task's verification passed.

---

### Task 1: API probe script + close out the findings doc

The `lastUpdated` semantics are ALREADY VERIFIED (Jamil, devtools, 2026-07-09) and recorded in `docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md`, which is the authority for pruning rules. Summary: org lastUpdated is frozen (never check it); project, asset-project, and asset-task lastUpdated are all reliable (asset-task bumps even for file-requirement upload/removal); file-requirement lastUpdated is UNRELIABLE, use its `metrics.fileUploadedCount` / `fileSubmittedCount` / `status` count fields instead; personnel lastUpdated reflects profile activity, never use it for pruning.

This task verifies only what remains open (listed in the findings doc's last section), using the three endpoints already exercised by `extract_daily_reports.py`:
- `GET /api/organizations/{org_id}/projects` (project rows)
- `GET /api/projects/{project_did}/assets` (asset rows)
- `GET /api/asset-projects/{asset_project_id}/asset-tasks` (task rows)

**Files:**
- Create: `swift_api_pipeline/probe_inc_asset_tasks.py`
- Modify: `docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md` (fill the "Still open" section)

**Interfaces:**
- Produces: the completed findings doc that Task 4's `FIELD_MAP` constants must match. Open items to answer: (a) exact payload key inventories for a TS project, (b) which asset field is the FK for the asset-tasks endpoint (expected `id`), (c) id uniqueness within project for assets and tasks, (d) which asset field carries the requirement count, (e) does DELETING a task bump the parent lastUpdated (submits/approvals/cancellations confirmed; hard delete untested; the weekly `--full-walk` stays mandatory until confirmed).

- [ ] **Step 1: Write the probe script**

```python
#!/usr/bin/env python3
"""One-off probe: dump hierarchy payload shapes for one TS project.

Writes key inventories (not full payloads, they may contain emails) to
stdout. Run locally with the pipeline .env present. Read-only against
Swift; touches no tables.

Usage: python probe_inc_asset_tasks.py [--project-number 13]
"""
import argparse
import json
from collections import Counter

from config import SCHEMA_REFERENCE, get_db
from extract_daily_reports import DailyReportsPipeline  # reuses _request/auth


def key_inventory(rows):
    keys = Counter()
    for r in rows:
        for k in r.keys():
            keys[k] += 1
    return dict(keys.most_common())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-number", type=int, default=13)
    args = ap.parse_args()

    db = get_db()
    proj = db.fetchrow(
        f"SELECT project_did, project_name FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects "
        f"WHERE project_number = $1", args.project_number)
    print(f"Project: {proj['project_name']} ({proj['project_did']})")

    pipe = DailyReportsPipeline()

    assets = pipe.fetch_assets(proj["project_did"])
    print(f"\nASSETS: {len(assets)} rows")
    print(json.dumps(key_inventory(assets), indent=2))
    ids = [a.get("id") for a in assets]
    print(f"asset id unique: {len(ids) == len(set(ids))}")
    print(f"assets with lastUpdated: {sum(1 for a in assets if a.get('lastUpdated'))}")

    sample = assets[0]
    tasks = pipe.fetch_tasks(sample["id"])
    print(f"\nTASKS for asset {sample.get('name')!r}: {len(tasks)} rows")
    print(json.dumps(key_inventory(tasks), indent=2))
    tids = [t.get("id") for t in tasks]
    print(f"task id unique within asset: {len(tids) == len(set(tids))}")
    print(f"tasks with lastUpdated: {sum(1 for t in tasks if t.get('lastUpdated'))}")

    # Project-level lastUpdated comes from the org projects listing
    from extract import SwiftAPIExtractor
    ex = SwiftAPIExtractor()
    orgs = ex.extract_organizations()
    for org in orgs:
        for p in ex.extract_projects(org["id"]):
            if p.get("id") == proj["project_did"] or p.get("did") == proj["project_did"]:
                print(f"\nPROJECT row keys: {sorted(p.keys())}")
                print(f"project lastUpdated present: {'lastUpdated' in p}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `cd swift_api_pipeline && python probe_inc_asset_tasks.py --project-number 13`
Expected: key inventories for all three levels, `lastUpdated` present at every level, ids unique. If the asset FK for `fetch_tasks` is not `id`, note the correct field.

ALSO inventory the `metrics` object at project and asset level for child-count fields (e.g. `taskCount`, `assetCount`). If asset metrics expose a task count, deletions become detectable from the parent's count mismatch alone (stored count vs fetched count triggers a descend + reconcile), which replaces the full-walk sweep at GC scale. Record what count fields exist in the findings doc.

- [ ] **Step 3: Fill the findings doc's "Still open" section**

Record in `docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md`: the three key inventories, the confirmed FK field, id-uniqueness results, requirement-count field name, and the DELETE-propagation test result (procedure: Jamil deletes one disposable test task in Swift; re-run the probe against its asset and compare `lastUpdated` before/after). If delete does NOT propagate, keep "weekly full-walk safety net REQUIRED" in the doc; Task 6 wires it either way behind a flag.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/probe_inc_asset_tasks.py docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md
git commit -m "probe: hierarchy payload shapes + lastUpdated semantics for inc asset tasks"
```

---

### Task 2: Migration 176, shadow tables

**Files:**
- Create: `swift_api_pipeline/migrations/176_inc_asset_tasks_shadow_tables.sql`

**Interfaces:**
- Produces: `data_raw.raw_asset_tasks_inc` (task payload archive keyed by task_did), `data_staging.stg_assets_inc` (asset level, the asset watermark store), `data_staging.stg_asset_tasks_inc` (mirror of `stg_asset_tasks` business columns + `last_updated`), used by Tasks 4-7. Watermark rows go in the EXISTING `pipeline.content_watermarks` (migration 175) as `pipeline_name = 'asset_tasks_inc/<project_did>'`, no new watermark table.

- [ ] **Step 1: Check the next free migration number**

Run: `ls swift_api_pipeline/migrations/ | sort -n -t_ -k1 | tail -3`
Expected: 175 is the highest; this file is 176. If not, renumber.

- [ ] **Step 2: Write the migration**

```sql
-- 176: shadow tables for the INCREMENTAL asset-tasks pipeline (TS13+ pilot).
-- Runs in parallel with the full-reload pipeline; nothing here is read or
-- written by the current pipeline. Loading contract: guarded upserts by
-- natural key + keep-list reconcile inside successfully-fetched scopes;
-- NO run_id sweeps. stg_asset_tasks_inc mirrors stg_asset_tasks business
-- columns so the drift audit can diff them directly. Cutover or teardown
-- is an explicit later decision.

create table data_raw.raw_asset_tasks_inc (
  task_did     text primary key,
  asset_did    text not null,
  project_did  text not null,
  data         jsonb not null,
  last_updated timestamptz,
  loaded_at    timestamptz not null default now()
);
create index idx_raw_asset_tasks_inc_project on data_raw.raw_asset_tasks_inc (project_did);
alter table data_raw.raw_asset_tasks_inc enable row level security;

create table data_staging.stg_assets_inc (
  asset_did              text primary key,
  project_did            text not null,
  asset_id               text,
  asset_name             text,
  asset_requirement_count integer,
  last_updated           timestamptz,
  loaded_at              timestamptz not null default now()
);
create index idx_stg_assets_inc_project on data_staging.stg_assets_inc (project_did);
alter table data_staging.stg_assets_inc enable row level security;

create table data_staging.stg_asset_tasks_inc (
  task_did                    text primary key,
  project_did                 text not null,
  project_status              text,
  asset_did                   text not null,
  asset_id                    text,
  asset_name                  text,
  asset_requirement_count     integer,
  task_name                   text,
  task_status                 text,
  task_scheduled              date,
  task_assigned_to_did        text,
  task_assigned_to_collection text,
  task_assigned_to_name       text,
  task_assigned_to_email      text,
  task_submitted_on           date,
  task_submitted_by_did       text,
  task_submitted_by_name      text,
  task_submitted_by_email     text,
  task_approved_on            date,
  task_approved_by_did        text,
  task_approved_by_name       text,
  task_approved_by_email      text,
  task_cancelled_on           date,
  task_cancelled_by_did       text,
  task_cancelled_by_name      text,
  task_cancelled_by_email     text,
  task_name_clean             text,
  last_updated                timestamptz,
  loaded_at                   timestamptz not null default now()
);
create index idx_stg_asset_tasks_inc_project on data_staging.stg_asset_tasks_inc (project_did);
create index idx_stg_asset_tasks_inc_asset on data_staging.stg_asset_tasks_inc (asset_did);
alter table data_staging.stg_asset_tasks_inc enable row level security;
```

- [ ] **Step 3: Apply to prod and verify**

Apply via the Supabase MCP / SQL editor, then:
Run: `SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE relname LIKE '%_inc' AND nspname IN ('data_raw','data_staging');`
Expected: 3 rows, all `relrowsecurity = true`.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/176_inc_asset_tasks_shadow_tables.sql
git commit -m "migration 176: shadow tables for incremental asset-tasks pilot"
```

---

### Task 3: db_tx.py, transaction-pooler DB module

**Files:**
- Create: `swift_api_pipeline/db_tx.py`
- Test: `swift_api_pipeline/tests/test_db_tx.py`

**Interfaces:**
- Produces: `get_tx_db()` returning the same sync-facade wrapper style as `db.py`'s `get_db()` (methods `fetch`, `fetchrow`, `fetchval`, `execute`, `executemany`), but connected to port 6543 with `statement_cache_size=0`. Consumed by Tasks 4-7. Read `db.py` first and mirror its wrapper class; only the connect kwargs differ:

- [ ] **Step 1: Read db.py and write db_tx.py**

Mirror `db.py`'s pool/wrapper structure with these differences (exact kwargs):

```python
# db_tx.py — Supabase TRANSACTION-mode pooler (port 6543).
# Differences from db.py (session mode):
#   - statement_cache_size=0 (PgBouncer transaction mode cannot hold
#     named prepared statements between transactions)
#   - min_size=0: hold NO idle server slots; transaction mode multiplexes
#   - no session-level SET; use SET LOCAL inside a transaction if needed
TX_POOLER_PORT = 6543

pool = await asyncpg.create_pool(
    host=os.environ["SUPABASE_HOST"],
    port=int(os.environ.get("SUPABASE_TX_PORT", TX_POOLER_PORT)),
    database=os.environ["SUPABASE_DB"],
    user=os.environ["SUPABASE_USER"],
    password=os.environ["SUPABASE_PASSWORD"],
    min_size=0,
    max_size=int(os.environ.get("TX_POOL_MAX_SIZE", "10")),
    statement_cache_size=0,
)
```

- [ ] **Step 2: Write the connectivity test**

```python
# tests/test_db_tx.py
"""Live smoke test for the transaction-pooler module (needs .env + WARP)."""
from db_tx import get_tx_db, close_tx_db


def test_tx_roundtrip():
    db = get_tx_db()
    assert db.fetchval("SELECT 1") == 1
    # two calls must survive statement re-preparation (no named-stmt cache)
    assert db.fetchval("SELECT $1::int + 1", 41) == 42
    assert db.fetchval("SELECT $1::int + 1", 1) == 2
    close_tx_db()
```

- [ ] **Step 3: Run the test**

Run: `cd swift_api_pipeline && python -m pytest tests/test_db_tx.py -v`
Expected: PASS (requires Cloudflare WARP on, like all local DB access).

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/db_tx.py swift_api_pipeline/tests/test_db_tx.py
git commit -m "db_tx: transaction-pooler module for the incremental pipeline workers"
```

---

### Task 4: Walker core, prune logic as pure functions

**Files:**
- Create: `swift_api_pipeline/extract_asset_tasks_inc.py`
- Test: `swift_api_pipeline/tests/test_inc_prune.py`

**Interfaces:**
- Produces: `plan_asset_visits(fetched_assets, stored_assets)` and `plan_task_writes(fetched_tasks, stored_tasks)` pure functions consumed by Task 5's walker; `FIELD_MAP` constants matching Task 1's findings doc; `epoch_to_ts(val)` (epoch millis to aware UTC datetime, None-safe).
- Consumes: nothing from other tasks (pure functions + constants only in this task).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_inc_prune.py
from datetime import datetime, timezone
from extract_asset_tasks_inc import plan_asset_visits, plan_task_writes, epoch_to_ts

T1 = 1751990400000  # older epoch millis
T2 = 1752076800000  # newer


def _stored(did, ts):
    return {"asset_did": did, "last_updated": epoch_to_ts(ts)}


def test_epoch_to_ts():
    assert epoch_to_ts(None) is None
    dt = epoch_to_ts(T1)
    assert dt.tzinfo is not None and dt.year >= 2025


def test_new_asset_is_visited():
    visits, missing = plan_asset_visits([{"id": "a1", "lastUpdated": T1}], {})
    assert [a["id"] for a in visits] == ["a1"] and missing == set()


def test_unchanged_asset_is_skipped():
    visits, missing = plan_asset_visits(
        [{"id": "a1", "lastUpdated": T1}], {"a1": _stored("a1", T1)})
    assert visits == [] and missing == set()


def test_changed_asset_is_visited():
    visits, _ = plan_asset_visits(
        [{"id": "a1", "lastUpdated": T2}], {"a1": _stored("a1", T1)})
    assert [a["id"] for a in visits] == ["a1"]


def test_deleted_asset_is_reported_missing():
    visits, missing = plan_asset_visits([], {"a1": _stored("a1", T1)})
    assert visits == [] and missing == {"a1"}


def test_task_writes_only_changed():
    fetched = [{"id": "t1", "lastUpdated": T1}, {"id": "t2", "lastUpdated": T2}]
    stored = {"t1": epoch_to_ts(T1), "t2": epoch_to_ts(T1)}
    writes, missing = plan_task_writes(fetched, stored)
    assert [t["id"] for t in writes] == ["t2"] and missing == set()


def test_task_deletion_detected():
    writes, missing = plan_task_writes([], {"t1": epoch_to_ts(T1)})
    assert writes == [] and missing == {"t1"}
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd swift_api_pipeline && python -m pytest tests/test_inc_prune.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement the pure core**

```python
#!/usr/bin/env python3
"""Incremental asset-tasks pipeline (TS13+ shadow pilot).

Walk: project (content_watermarks skip) -> asset list (stg_assets_inc is
the watermark) -> task list for changed assets only -> guarded upserts of
changed tasks + keep-list reconcile inside every successfully-fetched
scope. See docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md
and the API findings doc for field semantics.
"""
from datetime import datetime, timezone

# FIELD_MAP: single place where Swift payload keys are named. MUST match
# docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md; update
# here if the probe found different names.
FIELD_MAP = {
    "id": "id",
    "last_updated": "lastUpdated",
    "asset_name": "name",
    "asset_id": "assetId",
    "req_count": ("metrics", "reqCount"),
}


def epoch_to_ts(val):
    """Swift epoch millis -> aware UTC datetime (None-safe)."""
    if not val:
        return None
    return datetime.fromtimestamp(int(val) / 1000, tz=timezone.utc)


def plan_asset_visits(fetched_assets, stored_assets):
    """Decide which assets to descend into.

    stored_assets: {asset_did: {"last_updated": datetime|None, ...}} from
    stg_assets_inc for this project. Returns (visits, missing_dids):
    visits = fetched asset rows that are new or moved past the stored
    last_updated; missing_dids = stored dids absent from the fetch
    (deletion candidates, reconciled by the caller).
    """
    visits = []
    fetched_ids = set()
    for a in fetched_assets:
        did = a.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored = stored_assets.get(did)
        fetched_ts = epoch_to_ts(a.get(FIELD_MAP["last_updated"]))
        if stored is None or stored.get("last_updated") is None \
                or fetched_ts is None or fetched_ts > stored["last_updated"]:
            visits.append(a)
    missing = set(stored_assets) - fetched_ids
    return visits, missing


def plan_task_writes(fetched_tasks, stored_task_ts):
    """Same contract at task level. stored_task_ts: {task_did: last_updated}."""
    writes = []
    fetched_ids = set()
    for t in fetched_tasks:
        did = t.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored_ts = stored_task_ts.get(did)
        fetched_ts = epoch_to_ts(t.get(FIELD_MAP["last_updated"]))
        if stored_ts is None or fetched_ts is None or fetched_ts > stored_ts:
            writes.append(t)
    missing = set(stored_task_ts) - fetched_ids
    return writes, missing
```

Note the deliberate bias: an entity with a missing/unparseable `lastUpdated` is ALWAYS visited/written. Fail toward extra work, never toward staleness.

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_inc_prune.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks_inc.py swift_api_pipeline/tests/test_inc_prune.py
git commit -m "inc asset tasks: prune planning core (pure functions + tests)"
```

---

### Task 5: Walker IO, per-project walk with guarded upserts and reconcile

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks_inc.py` (append)

**Interfaces:**
- Consumes: `plan_asset_visits`, `plan_task_writes`, `FIELD_MAP`, `epoch_to_ts` (Task 4); `get_tx_db()` (Task 3); `DailyReportsPipeline.fetch_assets/fetch_tasks` request pattern (reuse the auth/_request helpers by importing the class or copying its `_request`, decide while reading, prefer import to copying).
- Produces: `walk_project(db, project, baseline=False) -> dict` stats consumed by Task 6's runner. Row mapping `task_to_stg_row(project, asset, task) -> tuple` in the exact column order of `stg_asset_tasks_inc`.

- [ ] **Step 1: Implement walk_project**

The per-project sequence (all SQL executemany batches of 500):

```python
STG_COLS = (
    "task_did, project_did, project_status, asset_did, asset_id, asset_name, "
    "asset_requirement_count, task_name, task_status, task_scheduled, "
    "task_assigned_to_did, task_assigned_to_collection, task_assigned_to_name, "
    "task_assigned_to_email, task_submitted_on, task_submitted_by_did, "
    "task_submitted_by_name, task_submitted_by_email, task_approved_on, "
    "task_approved_by_did, task_approved_by_name, task_approved_by_email, "
    "task_cancelled_on, task_cancelled_by_did, task_cancelled_by_name, "
    "task_cancelled_by_email, task_name_clean, last_updated"
)
# guarded upsert: only rewrite when business payload OR last_updated moved
UPSERT_TASK = f"""
INSERT INTO data_staging.stg_asset_tasks_inc ({STG_COLS}, loaded_at)
VALUES ({",".join(f"${i}" for i in range(1, 29))}, now())
ON CONFLICT (task_did) DO UPDATE SET
  project_status=EXCLUDED.project_status, asset_id=EXCLUDED.asset_id,
  asset_name=EXCLUDED.asset_name,
  asset_requirement_count=EXCLUDED.asset_requirement_count,
  task_name=EXCLUDED.task_name, task_status=EXCLUDED.task_status,
  task_scheduled=EXCLUDED.task_scheduled,
  task_assigned_to_did=EXCLUDED.task_assigned_to_did,
  task_assigned_to_collection=EXCLUDED.task_assigned_to_collection,
  task_assigned_to_name=EXCLUDED.task_assigned_to_name,
  task_assigned_to_email=EXCLUDED.task_assigned_to_email,
  task_submitted_on=EXCLUDED.task_submitted_on,
  task_submitted_by_did=EXCLUDED.task_submitted_by_did,
  task_submitted_by_name=EXCLUDED.task_submitted_by_name,
  task_submitted_by_email=EXCLUDED.task_submitted_by_email,
  task_approved_on=EXCLUDED.task_approved_on,
  task_approved_by_did=EXCLUDED.task_approved_by_did,
  task_approved_by_name=EXCLUDED.task_approved_by_name,
  task_approved_by_email=EXCLUDED.task_approved_by_email,
  task_cancelled_on=EXCLUDED.task_cancelled_on,
  task_cancelled_by_did=EXCLUDED.task_cancelled_by_did,
  task_cancelled_by_name=EXCLUDED.task_cancelled_by_name,
  task_cancelled_by_email=EXCLUDED.task_cancelled_by_email,
  task_name_clean=EXCLUDED.task_name_clean,
  last_updated=EXCLUDED.last_updated, loaded_at=now()
WHERE stg_asset_tasks_inc.last_updated IS DISTINCT FROM EXCLUDED.last_updated
   OR (stg_asset_tasks_inc.task_status, stg_asset_tasks_inc.task_name)
      IS DISTINCT FROM (EXCLUDED.task_status, EXCLUDED.task_name)
"""
```

`walk_project(db, project, baseline=False)` control flow:

1. If not baseline: `prev = content_watermarks['asset_tasks_inc/<project_did>']`; if project row's `lastUpdated` (from the org projects listing, passed in on `project`) is present and `<= prev`, return `{"skipped": True}` without any API call.
2. `assets = fetch_assets(project_did)` (on exception: log, return `{"ok": False}`; NEVER reconcile on a failed fetch).
3. `stored_assets = SELECT asset_did, last_updated FROM stg_assets_inc WHERE project_did=$1`.
4. `visits, missing_assets = plan_asset_visits(assets, stored_assets)`; in baseline mode `visits = assets`.
5. Upsert ALL fetched asset rows into `stg_assets_inc` with the same guarded pattern (guard on `last_updated`, `asset_name`, `asset_requirement_count`). Delete `missing_assets` rows AND their tasks (`DELETE FROM stg_asset_tasks_inc WHERE asset_did = ANY($1)`; same for `raw_asset_tasks_inc`).
6. Per visited asset: `tasks = fetch_tasks(asset_id)` (exception: log, count as failed asset, skip its reconcile); `stored = SELECT task_did, last_updated FROM stg_asset_tasks_inc WHERE asset_did=$1`; `writes, missing = plan_task_writes(tasks, stored)` (baseline: `writes = tasks`); executemany the guarded upsert for `writes` + `raw_asset_tasks_inc` payload upsert (guard: `data IS DISTINCT FROM EXCLUDED.data`); delete `missing` task rows.
7. Advance the project watermark to `max(lastUpdated seen)` ONLY if every fetch in the project succeeded.
8. Return stats: `{"ok": True, "assets": len(assets), "visited": len(visits), "task_writes": n, "task_deletes": n}`.

- [ ] **Step 2: Validate the SQL against prod (no data written)**

Run each statement with `EXPLAIN (COSTS OFF)` and dummy literals against the `_inc` tables (same technique as the 2026-07-09 daily-reports guards).
Expected: `Conflict Filter` visible on both upserts; no errors.

- [ ] **Step 3: Run the pure tests again (regression)**

Run: `cd swift_api_pipeline && python -m pytest tests/test_inc_prune.py -v`
Expected: still 7 passed. `python -m py_compile extract_asset_tasks_inc.py` clean.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks_inc.py
git commit -m "inc asset tasks: per-project walker with guarded upserts + keep-list reconcile"
```

---

### Task 6: Runner, workers, CLI, baseline mode

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks_inc.py` (append `main()`)

**Interfaces:**
- Consumes: `walk_project` (Task 5); `reference.ref_ontel_techops_projects` (project_number >= 13); project `lastUpdated` from `SwiftAPIExtractor.extract_organizations/extract_projects`.
- Produces: CLI `python extract_asset_tasks_inc.py [--baseline] [--full-walk] [--workers 6] [--project TS13]`; records runs in `pipeline.pipeline_runs` as `asset_tasks_inc` (reuse `SupabaseLoader.start_pipeline_run/complete_pipeline_run` pattern but through `db_tx`, copy the two small INSERT/UPDATE statements rather than importing the session-pool loader).

- [ ] **Step 1: Implement main()**

- Resolve projects (>= 13) and join each to its org-projects row for `lastUpdated` (one `extract_organizations` + `extract_projects` pass, same as the orgs pipeline does; if a project is missing from the listing, treat as changed).
- `--baseline`: pass `baseline=True` to every walk (full fetch + seed watermarks). `--full-walk`: baseline semantics for fetches but keep guards (the weekly ghost-sweep safety net; behavior identical to baseline since guards make re-writes no-ops).
- `ThreadPoolExecutor(max_workers=args.workers, default 6)`, one future per project, each worker uses the shared `get_tx_db()` facade.
- Log per-project stats lines and a final summary (projects skipped/walked, assets visited, task writes/deletes, failures). Exit non-zero if any project returned `ok: False`.

- [ ] **Step 2: Baseline run against prod (the pilot's run 1)**

Run: `cd swift_api_pipeline && python extract_asset_tasks_inc.py --baseline --workers 6`
Expected: completes; `SELECT count(*) FROM data_staging.stg_asset_tasks_inc` within ~1% of `SELECT count(*) FROM data_staging.stg_asset_tasks` (small drift = in-flight changes; the audit in Task 7 is the real check). Watermark rows exist: `SELECT count(*) FROM pipeline.content_watermarks WHERE pipeline_name LIKE 'asset_tasks_inc/%'` equals the project count.

- [ ] **Step 3: Immediate second run (the skip proof)**

Run: `cd swift_api_pipeline && python extract_asset_tasks_inc.py --workers 6`
Expected: most projects skipped at the watermark check or all-assets-pruned; task_writes near zero; runtime a small fraction of the baseline run. Capture both runtimes in the commit message.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks_inc.py
git commit -m "inc asset tasks: runner + workers + baseline/full-walk modes (baseline Xs, incremental Ys)"
```

---

### Task 7: Drift audit vs the current pipeline

**Files:**
- Create: `swift_api_pipeline/audit_asset_tasks_inc.py`

**Interfaces:**
- Consumes: `stg_asset_tasks` (current pipeline, read-only) and `stg_asset_tasks_inc` (Tasks 2-6).
- Produces: exit 0 when aligned, exit 1 with a per-project diff report when not; consumed by Task 8's workflow.

- [ ] **Step 1: Implement the audit**

Per project, entirely in SQL (one query each side, compare in Python):

```sql
SELECT project_did, count(*) AS rows,
       md5(string_agg(task_did || '|' || coalesce(task_status,'') || '|' ||
           coalesce(task_scheduled::text,'') || '|' || coalesce(task_name_clean,''),
           '|' ORDER BY task_did)) AS content_hash
FROM data_staging.stg_asset_tasks      -- and _inc on the other side
WHERE project_did = ANY($1)
GROUP BY project_did
```

Report per project: rows_current vs rows_inc, hash match yes/no. On mismatch, drill down: `SELECT task_did FROM ... EXCEPT SELECT task_did FROM ...` both directions (LIMIT 20 each) plus a sample of hash-relevant column diffs for shared task_dids. Print an EXPLICIT caveat line: the two pipelines run at different times, so small drift right after either run is expected; persistent same-direction drift is the bug signal.

- [ ] **Step 2: Run it after Task 6's baseline**

Run: `cd swift_api_pipeline && python audit_asset_tasks_inc.py`
Expected: counts within noise of each other per project; investigate any project off by more than in-flight churn before continuing.

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/audit_asset_tasks_inc.py
git commit -m "inc asset tasks: drift audit against current pipeline"
```

---

### Task 8: GHA workflow (manual dispatch pilot)

**Files:**
- Create: `.github/workflows/pipeline-asset-tasks-inc.yml`

**Interfaces:**
- Consumes: the CLI (Task 6) and audit (Task 7). Secrets/env identical to `pipeline-asset-tasks.yml` (copy its .env block) PLUS nothing new: `db_tx.py` reads the same SUPABASE_* vars, only the port constant differs.

- [ ] **Step 1: Write the workflow**

Structure (mirror `pipeline-asset-tasks.yml` boilerplate: checkout, python 3.12, pip cache, .env creation):

```yaml
name: "Pipeline: Asset Tasks (Incremental Shadow)"
on:
  workflow_dispatch:
    inputs:
      mode:
        description: "incremental | baseline | full-walk"
        required: false
        default: "incremental"
  repository_dispatch:
    types: [pipeline-asset-tasks-inc]
concurrency:
  group: pipeline-asset-tasks-inc
  cancel-in-progress: false
# jobs: single job, timeout-minutes: 45,
#   run: python -u extract_asset_tasks_inc.py $( [ "$MODE" = baseline ] && echo --baseline; [ "$MODE" = full-walk ] && echo --full-walk )
#   then: python -u audit_asset_tasks_inc.py   (continue-on-error: false)
#   upload pipeline_logs on failure like the other workflows
```

No cron and no Apps Script dispatcher yet: pilot runs are manual `gh workflow run`. Wiring the schedule happens AFTER a clean week, per the Apps-Script-for-scheduling convention, as two Apps Script triggers dispatching `pipeline-asset-tasks-inc`:
- daily incremental (`client_payload.mode = "incremental"`)
- **Sunday `--full-walk` ghost sweep** (`client_payload.mode = "full-walk"`, Sunday 06:00 PHT = Saturday 6 PM ET, outside the 1-10 PM PHT shift; decided by Jamil 2026-07-09). At TS13+ pilot scale a weekly full walk is cheap insurance. It does NOT scale to GC (15M+ rows); see Task 9 for the GC-scale deletion strategy.

- [ ] **Step 2: Dispatch once and watch it**

Run: `gh workflow run pipeline-asset-tasks-inc.yml -f mode=incremental && gh run watch`
Expected: green; log shows skip/visit stats; audit step passes.

- [ ] **Step 3: Commit + push**

```bash
git add .github/workflows/pipeline-asset-tasks-inc.yml
git commit -m "inc asset tasks: shadow workflow (manual dispatch pilot)"
git push origin main
```

---

### Task 9: Documentation + pilot exit criteria

**Files:**
- Modify: `README.md` (pipeline inventory section: add the shadow pipeline, one paragraph)
- Modify: `docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md` (this file: check off tasks, record baseline/incremental runtimes)

- [ ] **Step 1: Document**

README paragraph must state: shadow status, that the current pipeline remains authoritative, the audit command, the force-resync escape hatches (`--baseline`, or `DELETE FROM pipeline.content_watermarks WHERE pipeline_name LIKE 'asset_tasks_inc/%'`), and the pilot exit criteria below.

**Pilot exit criteria (the cutover/GC gate, decided by Jamil, not by this plan):**
1. 7+ consecutive daily runs green.
2. Drift audit clean (or explained by run-timing) every day.
3. Incremental runtime and IO a small fraction of the current pipeline's.
4. Delete-propagation behavior confirmed and covered (either natively or by the weekly full-walk).

Then: re-structure the current asset-tasks pipeline on this pattern and build gc-asset-tasks the same way (its data is mostly untouched daily, the best case for this design).

**GC-scale deletion strategy (15M+ rows; a weekly full walk does NOT scale there).** In priority order:
1. If Task 1's delete test shows hard deletes bump the parent `lastUpdated`: no sweep needed at any scale, the normal walk sees them.
2. If asset/project `metrics` expose child counts (Task 1 inventories this): detect deletions from count mismatch (stored vs fetched count on the CHEAP parent-list call), descend + reconcile only mismatched scopes. Near-free at any scale.
3. Fallback only: rotating partial sweep, 1/Nth of assets per night round-robin so full coverage every N days without any single heavy run. The Sunday full walk stays a pilot-scale tool.

- [ ] **Step 2: Commit + push**

```bash
git add README.md docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md
git commit -m "docs: incremental asset-tasks shadow pilot + exit criteria"
git push origin main
```
