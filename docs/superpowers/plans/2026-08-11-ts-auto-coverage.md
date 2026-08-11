# TS Project Auto-Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New TS projects are picked up automatically by every consumer: the three nightly export scripts read the project list from the DB, and new QA forms self-register via the Swift REST forms endpoint.

**Architecture:** `reference.ref_ontel_techops_projects` (existing view) stays the single source of truth for TS projects. QA form config moves from `config.py QA_FORMS` to a new seeded `reference.ref_qa_forms` table. A discovery step in the nightly forms pipeline fetches `GET /api/organizations/-K5UFaiZw8e3-7nii3eT/forms` and registers `ACTIVE - QA Form TS{n}` forms for any unregistered TS. Spec: `docs/superpowers/specs/2026-08-11-ts-project-auto-coverage-design.md`.

**Tech Stack:** Python 3.12, asyncpg (exports), psycopg-style sync `db.py` helpers (pipeline), requests (Swift REST), Gmail API via `gmail_client.authenticate()` (emails), pytest.

## Global Constraints

- Work on branch `feat/ts-auto-coverage`; merge to main only via PR after the `premerge-review` skill (standing habit).
- All DB objects in our schemas only; new table goes in `reference`. RLS enabled on every new table (standing rule; disabling needs Jamil's permission).
- Migration numbering: highest in `swift_api_pipeline/migrations/` is `230_*` as of 2026-08-11 — **list the dir at execution time and take the next free number** (assumed `231` below; renumber everywhere if taken).
- Ad-hoc / alert emails go ONLY to `jamil.mendez@ontel.co`.
- Do not touch the inc-shadow walker's TS17–19 pilot scope.
- Exact-match form title pattern: `^ACTIVE - QA Form TS(\d+)$` (case-sensitive).
- Ontel org DID: `-K5UFaiZw8e3-7nii3eT`. Min TS number everywhere: `13`.
- Windows dev machine: run tests with `./venv/Scripts/python.exe -m pytest` from `swift_api_pipeline/`.
- `test_asset_tasks_resilience.py` has 6 known pre-existing failures (email-wiring tests). They are NOT yours to fix; judge your changes by "no NEW failures".

---

### Task 1: Branch + commit the discover_forms login fix + delete spike artifacts

`discover_forms.py` has uncommitted login fixes from the 2026-08-11 spike (Enter-to-submit + wait for the login form to disappear). Spike scripts/screenshots are throwaway.

**Files:**
- Modify (already edited, uncommitted): `swift_api_pipeline/discover_forms.py`
- Delete (untracked): `swift_api_pipeline/_spike_org_nav.py`, `swift_api_pipeline/_spike_forms_api.py`, `swift_api_pipeline/_spike_nav*.png`, `swift_api_pipeline/forms_page_debug.png`, `swift_api_pipeline/login_debug.png`

**Interfaces:**
- Produces: branch `feat/ts-auto-coverage` for all later tasks.

- [ ] **Step 1: Create the branch (from current main)**

```bash
cd local-pipeline && git checkout -b feat/ts-auto-coverage
```

- [ ] **Step 2: Verify the discover_forms diff is only the two login blocks**

Run: `git diff swift_api_pipeline/discover_forms.py`
Expected: Enter-submit block + `wait_for_selector('input[type="email"]', state="hidden", ...)` block; nothing else.

- [ ] **Step 3: Delete spike artifacts**

```bash
rm swift_api_pipeline/_spike_org_nav.py swift_api_pipeline/_spike_forms_api.py
rm -f swift_api_pipeline/_spike_nav*.png swift_api_pipeline/forms_page_debug.png swift_api_pipeline/login_debug.png
```

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/discover_forms.py
git commit -m "fix(discover-forms): submit login via Enter + wait for login form to disappear"
```

---

### Task 2: Migration — `reference.ref_qa_forms` (seed + RLS)

**Files:**
- Create: `swift_api_pipeline/migrations/231_ref_qa_forms.sql`

**Interfaces:**
- Produces: table `reference.ref_qa_forms(ts_number int PK, form_id text UNIQUE, form_title text, table_name text UNIQUE, active bool, registered_by text, registered_at timestamptz)` consumed by Tasks 3, 6, 7, 8.

- [ ] **Step 1: Confirm next free migration number**

Run: `ls swift_api_pipeline/migrations/ | sort -n | tail -3`
Expected: highest is `230_*`. If not, renumber this file and every later reference.

- [ ] **Step 2: Write the migration**

```sql
-- Migration 231: reference.ref_qa_forms — QA form registry (replaces config.py QA_FORMS)
-- New TS projects get their QA form auto-registered by the nightly forms pipeline
-- (see docs/superpowers/specs/2026-08-11-ts-project-auto-coverage-design.md).

CREATE TABLE reference.ref_qa_forms (
    ts_number     INTEGER PRIMARY KEY CHECK (ts_number >= 13),
    form_id       TEXT NOT NULL UNIQUE,
    form_title    TEXT NOT NULL,
    table_name    TEXT NOT NULL UNIQUE,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    registered_by TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE reference.ref_qa_forms ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON reference.ref_qa_forms FROM anon, authenticated;
GRANT ALL ON reference.ref_qa_forms TO service_role;

INSERT INTO reference.ref_qa_forms (ts_number, form_id, form_title, table_name, registered_by) VALUES
    (13, '-NH1hUPkaKtPdd7BK9cb', 'ACTIVE - QA Form TS13', 'raw_form_qa_ts13', 'seed'),
    (14, '-NXCg4vTDNVykN8ioMYp', 'ACTIVE - QA Form TS14', 'raw_form_qa_ts14', 'seed'),
    (15, '-Np6o9OCL4RWIJq68HJe', 'ACTIVE - QA Form TS15', 'raw_form_qa_ts15', 'seed'),
    (16, '-O9ACLN3je1w7oEoG5hY', 'ACTIVE - QA Form TS16', 'raw_form_qa_ts16', 'seed'),
    (17, '-ONMD-cGBq-_3r9ybaAq', 'ACTIVE - QA Form TS17', 'raw_form_qa_ts17', 'seed'),
    (18, '-O_J2hPlryTezP9RhujA', 'ACTIVE - QA Form TS18', 'raw_form_qa_ts18', 'seed'),
    (19, '-Omun_NWXeQE1tEhSPXf', 'ACTIVE - QA Form TS19', 'raw_form_qa_ts19', 'seed');
```

- [ ] **Step 3: Senior-dev preflight (standing rule for live DB changes)**

Run these against the cloud DB (Supabase MCP `execute_sql`, project `voqfjfngdpcvevbkikud`) BEFORE applying:

```sql
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'reference' AND table_name = 'ref_qa_forms';
-- expect: 0 rows (doesn't exist yet)
```

Also verify the seven seed form_ids match `config.py QA_FORMS` values verbatim (open the file and compare each).

- [ ] **Step 4: Apply via Supabase MCP `apply_migration` (name: `231_ref_qa_forms`)**

- [ ] **Step 5: Verify**

```sql
SELECT ts_number, form_id, table_name, active FROM reference.ref_qa_forms ORDER BY ts_number;
-- expect: 7 rows, TS13..TS19, active = true
SELECT relrowsecurity FROM pg_class
WHERE oid = 'reference.ref_qa_forms'::regclass;  -- expect: true
```

- [ ] **Step 6: Commit**

```bash
git add swift_api_pipeline/migrations/231_ref_qa_forms.sql
git commit -m "migration 231: reference.ref_qa_forms registry (seed TS13-19, RLS)"
```

---

### Task 3: `ts_projects.py` — shared dynamic-project helpers + tests

Pure logic lives here so all three export scripts (which already `sys.path.insert` the pipeline dir) can import it, and so it's unit-testable.

**Files:**
- Create: `swift_api_pipeline/ts_projects.py`
- Test: `swift_api_pipeline/tests/test_ts_projects.py`

**Interfaces:**
- Produces (consumed by Tasks 4, 5, 6):
  - `async fetch_ts_projects(conn, min_number=13) -> list[dict]` — rows `{"project_name": str, "project_did": str, "project_number": int}` from the ref view, ordered by number. Raises on query failure (callers abort — no stale fallback).
  - `async fetch_qa_export_projects(conn) -> list[tuple[str, str]]` — `(project_name, project_did)` for TSes registered in `ref_qa_forms` (active only), ordered by number.
  - `partition_by_rows(projects: list[str], counts: dict[str, int]) -> tuple[list[str], list[str]]` — `(with_rows, empty)`; a project missing from `counts` counts as empty.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for ts_projects helpers (dynamic TS project lists)."""
import asyncio
import pytest

from ts_projects import fetch_ts_projects, fetch_qa_export_projects, partition_by_rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_args = None

    async def fetch(self, query, *args):
        self.last_query = " ".join(query.split())
        self.last_args = args
        return self.rows


def test_fetch_ts_projects_maps_rows_and_filters_by_number():
    rows = [
        {"project_name": "TECH-OPS: TS13", "project_did": "-A", "project_number": 13},
        {"project_name": "TECH-OPS: TS20", "project_did": "-B", "project_number": 20},
    ]
    conn = FakeConn(rows)
    out = asyncio.run(fetch_ts_projects(conn))
    assert out == rows
    assert "ref_ontel_techops_projects" in conn.last_query
    assert "project_number >= $1" in conn.last_query
    assert conn.last_args == (13,)


def test_fetch_ts_projects_custom_min_number():
    conn = FakeConn([])
    asyncio.run(fetch_ts_projects(conn, min_number=17))
    assert conn.last_args == (17,)


def test_fetch_qa_export_projects_joins_registry():
    rows = [{"project_name": "TECH-OPS: TS13", "project_did": "-A"}]
    conn = FakeConn(rows)
    out = asyncio.run(fetch_qa_export_projects(conn))
    assert out == [("TECH-OPS: TS13", "-A")]
    assert "ref_qa_forms" in conn.last_query
    assert "active" in conn.last_query


def test_partition_by_rows_splits_empty_projects():
    with_rows, empty = partition_by_rows(
        ["TS13", "TS19", "TS20"], {"TS13": 100, "TS19": 5, "TS20": 0}
    )
    assert with_rows == ["TS13", "TS19"]
    assert empty == ["TS20"]


def test_partition_by_rows_missing_count_is_empty():
    with_rows, empty = partition_by_rows(["TS13", "TS20"], {"TS13": 1})
    assert empty == ["TS20"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_ts_projects.py -v` (from `swift_api_pipeline/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'ts_projects'`

- [ ] **Step 3: Implement**

```python
"""Dynamic TS project lists — single source of truth for TS-scoped consumers.

reference.ref_ontel_techops_projects is a view over stg_projects that derives
project_number from the name, so new TS projects appear automatically after
each projects-pipeline run. Never hardcode a TS list; query it here.
"""

MIN_TS_NUMBER = 13

_TS_PROJECTS_QUERY = """
    SELECT project_name, project_did, project_number
    FROM reference.ref_ontel_techops_projects
    WHERE project_number >= $1
    ORDER BY project_number
"""

_QA_EXPORT_QUERY = """
    SELECT p.project_name, p.project_did
    FROM reference.ref_qa_forms f
    JOIN reference.ref_ontel_techops_projects p ON p.project_number = f.ts_number
    WHERE f.active
    ORDER BY f.ts_number
"""


async def fetch_ts_projects(conn, min_number=MIN_TS_NUMBER):
    rows = await conn.fetch(_TS_PROJECTS_QUERY, min_number)
    return [dict(r) for r in rows]


async def fetch_qa_export_projects(conn):
    rows = await conn.fetch(_QA_EXPORT_QUERY)
    return [(r["project_name"], r["project_did"]) for r in rows]


def partition_by_rows(projects, counts):
    with_rows = [p for p in projects if counts.get(p, 0) > 0]
    empty = [p for p in projects if counts.get(p, 0) <= 0]
    return with_rows, empty
```

(Note: `dict(r)` on a plain dict is a no-op, and on an asyncpg `Record` produces a dict — both fine.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_ts_projects.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/ts_projects.py swift_api_pipeline/tests/test_ts_projects.py
git commit -m "feat(ts-projects): shared dynamic TS project helpers"
```

---

### Task 4: `export_asset_tasks_excel.py` goes dynamic + skip-empty guard

**Files:**
- Modify: `scripts-reference/export_asset_tasks_excel.py` (constants ~line 43; `check_pipeline_guard` ~lines 123–191; export loop ~lines 305–330)

**Interfaces:**
- Consumes: `fetch_ts_projects`, `partition_by_rows` from `ts_projects` (Task 3).
- Produces: guard returns the list of project names to export (non-empty ones).

- [ ] **Step 1: Replace the hardcoded list**

Delete the `PROJECTS = [...]` literal. The script already does `sys.path.insert(0, str(PIPELINE_DIR))`; add:

```python
from ts_projects import fetch_ts_projects, partition_by_rows
```

- [ ] **Step 2: Rework the guard**

In `check_pipeline_guard(conn)`: keep the pipeline_runs status/error check verbatim (unchanged — it still blocks on a stamped error note). Replace the "Verify all 6 projects have rows" block: fetch projects dynamically, count rows per project, and skip empties instead of failing:

```python
    projects = await fetch_ts_projects(conn)
    names = [p["project_name"] for p in projects]

    project_counts = await conn.fetch("""
        SELECT p.project_name, COUNT(at.id) AS row_count
        FROM data_staging.stg_projects p
        LEFT JOIN data_staging.stg_asset_tasks at ON at.project_did = p.project_did
        WHERE p.project_name = ANY($1::text[])
        GROUP BY p.project_name
        ORDER BY p.project_name
    """, names)

    counts = {r["project_name"]: r["row_count"] for r in project_counts}
    with_rows, empty = partition_by_rows(names, counts)

    for name in with_rows:
        print(f"  {name}: {counts[name]:,} rows")
    for name in empty:
        print(f"  {name}: SKIPPED (new/empty - no staging rows yet)")

    if not with_rows:
        raise SystemExit("GUARD FAILED: no TS project has any rows in stg_asset_tasks. Aborting export.")
```

Keep the raw-vs-staging total count check verbatim. Change the function to `return with_rows` and update its caller to use that returned list wherever `PROJECTS` was used (the docstring "all 6 projects" line and the module docstring mention of TS13–TS19 get updated too).

Rationale (from spec): "data was there and shrank" alarms are the extract guard's job since `9168639`; the export no longer duplicates that judgment.

- [ ] **Step 3: Update the export loop**

Where the per-project loop iterated `PROJECTS`, iterate the guard's returned list. Verify with `grep -n "PROJECTS" scripts-reference/export_asset_tasks_excel.py` → expect zero hits when done.

- [ ] **Step 4: Syntax check**

Run: `./venv/Scripts/python.exe -m py_compile ../scripts-reference/export_asset_tasks_excel.py`
Expected: exit 0. (Full E2E happens on the nightly run; the guard SQL was exercised live during the 2026-08-11 incident.)

- [ ] **Step 5: Commit**

```bash
git add scripts-reference/export_asset_tasks_excel.py
git commit -m "feat(export-asset-tasks): dynamic TS list from ref view, skip empty new projects"
```

---

### Task 5: `export_timer_excel.py` goes dynamic

**Files:**
- Modify: `scripts-reference/export_timer_excel.py` (delete `PROJECTS` literal ~lines 41–49; loop ~line 317)

**Interfaces:**
- Consumes: `fetch_ts_projects` from `ts_projects` (Task 3).

- [ ] **Step 1: Replace the list**

Delete the `PROJECTS` literal, add `from ts_projects import fetch_ts_projects`. Where the script first has an open asyncpg connection (before the per-project loop), fetch:

```python
    projects = [p["project_name"] for p in await fetch_ts_projects(conn)]
```

and iterate `projects` in the loop at ~line 317 (`for i, project_name in enumerate(projects):`). A project with no timer rows simply yields an empty/absent workbook exactly as it would today — no guard change needed in this script. Update the TS13–TS18/TS19 mentions in the module docstring.

- [ ] **Step 2: Syntax check**

Run: `./venv/Scripts/python.exe -m py_compile ../scripts-reference/export_timer_excel.py`
Expected: exit 0. `grep -n "PROJECTS" ../scripts-reference/export_timer_excel.py` → zero hits.

- [ ] **Step 3: Commit**

```bash
git add scripts-reference/export_timer_excel.py
git commit -m "feat(export-timer): dynamic TS list from ref view"
```

---

### Task 6: `export_qa_form_excel.py` sources from `ref_qa_forms`

**Files:**
- Modify: `scripts-reference/export_qa_form_excel.py` (delete `PROJECTS` pairs ~lines 41–49; loop ~lines 241–254)

**Interfaces:**
- Consumes: `fetch_qa_export_projects` from `ts_projects` (Task 3) → `[(project_name, project_did), ...]`.

- [ ] **Step 1: Replace the pairs list**

Delete the `PROJECTS` literal, add `from ts_projects import fetch_qa_export_projects`. After the script opens its asyncpg connection:

```python
    projects = await fetch_qa_export_projects(conn)
```

The loop already unpacks `(project_name, project_did)` tuples — iterate `projects` instead of `PROJECTS` (including the look-ahead prefetch at indices `[0]` and `[i + 1]`). A TS with no registered QA form is simply absent until discovery registers it (spec section 1).

- [ ] **Step 2: Syntax check**

Run: `./venv/Scripts/python.exe -m py_compile ../scripts-reference/export_qa_form_excel.py`
Expected: exit 0; `grep -n '"-N' ../scripts-reference/export_qa_form_excel.py` → zero hits (no hardcoded dids left).

- [ ] **Step 3: Commit**

```bash
git add scripts-reference/export_qa_form_excel.py
git commit -m "feat(export-qa-form): dynamic list from ref_qa_forms registry"
```

---

### Task 7: `qa_forms_registry.py` — pipeline reads `ref_qa_forms`; delete `config.QA_FORMS`

**Files:**
- Create: `swift_api_pipeline/qa_forms_registry.py`
- Test: `swift_api_pipeline/tests/test_qa_forms_registry.py`
- Modify: `swift_api_pipeline/extract_forms.py` (import ~line 18; `clear_old_raw_data` ~line 206; `run_forms_pipeline` default ~line 223), `swift_api_pipeline/transform.py` (import ~line 13; UNION build ~line 727; validation ~line 883), `swift_api_pipeline/config.py` (delete `QA_FORMS` ~lines 51–88)

**Interfaces:**
- Consumes: `reference.ref_qa_forms` (Task 2); the pipeline's sync `db` object (`db.fetch(query, *args)` — same call shape used by `AssetTaskExtractor.get_project_dids`).
- Produces: `load_qa_forms(db) -> dict` in the EXACT legacy shape consumers already use:
  `{"qa_ts13": {"form_id": "...", "table_name": "raw_form_qa_ts13", "display_name": "QA Form TS13"}, ...}`.
  Also `row_to_entry(ts_number, form_id) -> (key, entry_dict)` (pure, reused by discovery in Task 8).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for qa_forms_registry (DB-backed replacement of config.QA_FORMS)."""
from qa_forms_registry import load_qa_forms, row_to_entry


class FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None

    def fetch(self, query, *args):
        self.last_query = " ".join(query.split())
        return self.rows


def test_row_to_entry_derives_key_table_and_display_name():
    key, entry = row_to_entry(20, "-Oxyz12345678901234")
    assert key == "qa_ts20"
    assert entry == {
        "form_id": "-Oxyz12345678901234",
        "table_name": "raw_form_qa_ts20",
        "display_name": "QA Form TS20",
    }


def test_load_qa_forms_returns_legacy_shape_ordered():
    rows = [
        {"ts_number": 13, "form_id": "-A"},
        {"ts_number": 20, "form_id": "-B"},
    ]
    db = FakeDb(rows)
    forms = load_qa_forms(db)
    assert list(forms.keys()) == ["qa_ts13", "qa_ts20"]
    assert forms["qa_ts13"]["table_name"] == "raw_form_qa_ts13"
    assert "ref_qa_forms" in db.last_query
    assert "active" in db.last_query


def test_load_qa_forms_empty_registry_raises():
    import pytest
    db = FakeDb([])
    try:
        load_qa_forms(db)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ref_qa_forms" in str(e)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_qa_forms_registry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
"""DB-backed QA forms registry — replaces the config.py QA_FORMS dict.

Source of truth: reference.ref_qa_forms (migration 231), maintained by seed +
the nightly auto-discovery step (qa_form_discovery.py). Consumers get the same
dict shape the legacy QA_FORMS constant had, so extract/transform code is
unchanged beyond the lookup.
"""
from config import SCHEMA_REFERENCE

_QUERY = f"""
    SELECT ts_number, form_id
    FROM {SCHEMA_REFERENCE}.ref_qa_forms
    WHERE active
    ORDER BY ts_number
"""


def row_to_entry(ts_number, form_id):
    key = f"qa_ts{ts_number}"
    return key, {
        "form_id": form_id,
        "table_name": f"raw_form_qa_ts{ts_number}",
        "display_name": f"QA Form TS{ts_number}",
    }


def load_qa_forms(db):
    rows = db.fetch(_QUERY)
    if not rows:
        raise RuntimeError(
            "reference.ref_qa_forms returned no active rows - refusing to run "
            "the forms pipeline against an empty registry"
        )
    forms = {}
    for r in rows:
        key, entry = row_to_entry(r["ts_number"], r["form_id"])
        forms[key] = entry
    return forms
```

(Check `SCHEMA_REFERENCE` exists in config.py — the extractors already use it. If the constant has a different name, use that one.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_qa_forms_registry.py -v`
Expected: 3 PASS

- [ ] **Step 5: Swap consumers**

Find every QA_FORMS use: `grep -rn "QA_FORMS" swift_api_pipeline/ --include="*.py" | grep -v venv | grep -v migrations`

- `extract_forms.py`: remove `QA_FORMS` from the config import; in `run_forms_pipeline(forms=None, ...)` replace the `forms = QA_FORMS` default with `forms = load_qa_forms(extractor.db)` — note this must run AFTER the extractor is constructed; move the default resolution to just after `extractor = FormsExtractor()`. In `clear_old_raw_data`, the method iterates `QA_FORMS.values()` — change it to accept the forms dict: `def clear_old_raw_data(self, forms)` and pass it from the caller.
- `transform.py`: remove the config import; both `transform_qa_forms` (UNION build, line ~727) and the validation block (line ~883) run with a `db` handle in scope — call `load_qa_forms(db)` once near the top of each function and iterate that.
- `config.py`: delete the `QA_FORMS` dict and its comment block.

- [ ] **Step 6: Verify nothing still imports the constant**

Run: `grep -rn "QA_FORMS" swift_api_pipeline/ --include="*.py" | grep -v venv | grep -v migrations | grep -v qa_forms_registry`
Expected: zero hits. Then `./venv/Scripts/python.exe -m py_compile extract_forms.py transform.py config.py` → exit 0.

- [ ] **Step 7: Run the full pipeline test suite (no new failures)**

Run: `./venv/Scripts/python.exe -m pytest tests/ -q`
Expected: same failure set as on the branch base (the 6 known email-wiring failures), nothing new.

- [ ] **Step 8: Commit**

```bash
git add swift_api_pipeline/qa_forms_registry.py swift_api_pipeline/tests/test_qa_forms_registry.py swift_api_pipeline/extract_forms.py swift_api_pipeline/transform.py swift_api_pipeline/config.py
git commit -m "feat(qa-forms): read registry from ref_qa_forms; delete config.QA_FORMS"
```

---

### Task 8: `qa_form_discovery.py` — REST discovery, registration, emails

**Files:**
- Create: `swift_api_pipeline/qa_form_discovery.py`
- Test: `swift_api_pipeline/tests/test_qa_form_discovery.py`

**Interfaces:**
- Consumes: `row_to_entry` (Task 7); bearer token (caller passes `extractor.token`); sync `db` (`db.fetch`, `db.execute`); `gmail_client.authenticate()` (same pattern as `pipeline_health_watcher.send_email`); `SWIFT_BASE_URL` from config.
- Produces (consumed by Task 9): `run_discovery(db, token, send_email=True) -> list[int]` — registers what it can, emails as needed, returns newly registered ts_numbers.
- Pure decision core (tested): `match_qa_form(forms, ts_number) -> dict` with `{"status": "one"|"zero"|"many", "matches": [...]}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for QA form auto-discovery decision logic."""
from qa_form_discovery import match_qa_form, missing_ts_numbers, needs_escalation

FORMS = [
    {"id": "-A1", "title": "ACTIVE - QA Form TS19"},
    {"id": "-A2", "title": "ACTIVE - QA Form TS20"},
    {"id": "-A3", "title": "QA Rejection Form"},
    {"id": "-A4", "title": "Option 1 of QA"},
    {"id": "-A5", "title": "ACTIVE - QA Subcategories - Original DO NOT PULL"},
]


def test_match_exactly_one():
    r = match_qa_form(FORMS, 20)
    assert r["status"] == "one"
    assert r["matches"] == [{"id": "-A2", "title": "ACTIVE - QA Form TS20"}]


def test_match_zero_when_form_absent():
    assert match_qa_form(FORMS, 21)["status"] == "zero"


def test_match_ignores_lookalikes_and_substring_numbers():
    # TS2 must NOT match TS20's title
    assert match_qa_form(FORMS, 2)["status"] == "zero"


def test_match_many_when_duplicated():
    forms = FORMS + [{"id": "-B2", "title": "ACTIVE - QA Form TS20"}]
    r = match_qa_form(forms, 20)
    assert r["status"] == "many"
    assert len(r["matches"]) == 2


def test_missing_ts_numbers():
    projects = [{"project_number": 13}, {"project_number": 19}, {"project_number": 20}]
    registered = {13, 19}
    assert missing_ts_numbers(projects, registered) == [20]


def test_needs_escalation_only_after_7_days_with_tasks():
    assert needs_escalation(task_count=5, project_age_days=8) is True
    assert needs_escalation(task_count=5, project_age_days=3) is False
    assert needs_escalation(task_count=0, project_age_days=30) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/Scripts/python.exe -m pytest tests/test_qa_form_discovery.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
"""Auto-discover and register QA forms for new TS projects.

Spike-proven 2026-08-11: GET /api/organizations/{ONTEL_ORG_DID}/forms returns
all org forms (paginated) with standard bearer auth; QA form titles follow the
strict pattern 'ACTIVE - QA Form TS{n}'. Runs at the top of the nightly forms
pipeline. Failure of any kind degrades to an alert email - never blocks the
extraction of already-registered forms.

Deliberate deviation from every-object-via-migration: the raw table for a
newly discovered form is created from RAW_TABLE_DDL below (version-controlled
template) - same precedent as asset-tasks partition auto-creation.
"""
import base64
import re
from email.mime.text import MIMEText

import requests

from config import SWIFT_BASE_URL, SCHEMA_RAW, SCHEMA_REFERENCE, get_logger
from qa_forms_registry import row_to_entry

logger = get_logger("qa_form_discovery")

ONTEL_ORG_DID = "-K5UFaiZw8e3-7nii3eT"
TITLE_PATTERN = re.compile(r"^ACTIVE - QA Form TS(\d+)$")
MIN_TS_NUMBER = 13
ESCALATION_AGE_DAYS = 7
ALERT_RECIPIENT = "jamil.mendez@ontel.co"

RAW_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.{table} (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);
ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON {schema}.{table} FROM anon, authenticated;
"""


# ---------- pure decision core ----------

def match_qa_form(forms, ts_number):
    matches = [
        {"id": f["id"], "title": f["title"]}
        for f in forms
        if (m := TITLE_PATTERN.match(f.get("title") or ""))
        and int(m.group(1)) == ts_number
    ]
    status = "one" if len(matches) == 1 else ("zero" if not matches else "many")
    return {"status": status, "matches": matches}


def missing_ts_numbers(projects, registered):
    return sorted(
        p["project_number"] for p in projects
        if p["project_number"] not in registered
    )


def needs_escalation(task_count, project_age_days):
    return task_count > 0 and project_age_days > ESCALATION_AGE_DAYS


# ---------- IO ----------

def fetch_org_forms(token):
    url = f"{SWIFT_BASE_URL}/api/organizations/{ONTEL_ORG_DID}/forms"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"pageSize": "100"}
    forms = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("list", [])
        forms.extend(batch)
        if not data.get("hasMore") or not batch:
            return forms
        params["after"] = batch[-1]["id"]


def send_alert(subject, body):
    from gmail_client import authenticate
    service = authenticate()
    msg = MIMEText(body, "plain")
    msg["To"] = ALERT_RECIPIENT
    msg["From"] = "me"
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Alert sent: {subject}")


def register_qa_form(db, ts_number, form_id, form_title):
    _, entry = row_to_entry(ts_number, form_id)
    ddl = RAW_TABLE_DDL.format(schema=SCHEMA_RAW, table=entry["table_name"])
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        db.execute(stmt)
    db.execute(
        f"INSERT INTO {SCHEMA_REFERENCE}.ref_qa_forms "
        f"(ts_number, form_id, form_title, table_name, registered_by) "
        f"VALUES ($1, $2, $3, $4, 'auto-discovery') "
        f"ON CONFLICT (ts_number) DO NOTHING",
        ts_number, form_id, form_title, entry["table_name"],
    )
    logger.info(f"Registered QA form TS{ts_number}: {form_id} -> {entry['table_name']}")


def _project_state(db, ts_number):
    rows = db.fetch(
        f"SELECT p.project_did, "
        f"       COALESCE(EXTRACT(EPOCH FROM (NOW() - sp.date_created)) / 86400, 0) AS age_days, "
        f"       (SELECT COUNT(*) FROM data_staging.stg_asset_tasks t "
        f"        WHERE t.project_did = p.project_did) AS task_count "
        f"FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects p "
        f"JOIN data_staging.stg_projects sp ON sp.project_did = p.project_did "
        f"WHERE p.project_number = $1",
        ts_number,
    )
    return rows[0] if rows else None


def run_discovery(db, token, send_email=True):
    """Register QA forms for unregistered TS projects. Returns new ts_numbers."""
    projects = db.fetch(
        f"SELECT project_number FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects "
        f"WHERE project_number >= $1", MIN_TS_NUMBER,
    )
    registered = {
        r["ts_number"] for r in
        db.fetch(f"SELECT ts_number FROM {SCHEMA_REFERENCE}.ref_qa_forms")
    }
    missing = missing_ts_numbers(projects, registered)
    if not missing:
        return []

    forms = fetch_org_forms(token)
    newly = []
    for ts in missing:
        result = match_qa_form(forms, ts)
        if result["status"] == "one":
            m = result["matches"][0]
            register_qa_form(db, ts, m["id"], m["title"])
            newly.append(ts)
            if send_email:
                send_alert(
                    f"[forms] Registered QA Form TS{ts} automatically",
                    f"Auto-discovery registered QA Form TS{ts}:\n\n"
                    f"  form_id: {m['id']}\n  title:   {m['title']}\n"
                    f"  table:   raw_form_qa_ts{ts}\n\n"
                    f"It is included in tonight's forms extraction. "
                    f"Reply/flag if this is wrong - deactivate with:\n"
                    f"  UPDATE reference.ref_qa_forms SET active=false WHERE ts_number={ts};",
                )
        elif result["status"] == "many":
            if send_email:
                lines = "\n".join(f"  {m['id']}  {m['title']}" for m in result["matches"])
                send_alert(
                    f"[forms] QA Form TS{ts}: multiple candidates - manual pick needed",
                    f"Auto-discovery found {len(result['matches'])} candidate forms for TS{ts}:\n\n"
                    f"{lines}\n\nInsert the right one:\n"
                    f"  INSERT INTO reference.ref_qa_forms (ts_number, form_id, form_title, table_name, registered_by)\n"
                    f"  VALUES ({ts}, '<form_id>', '<title>', 'raw_form_qa_ts{ts}', 'manual');",
                )
        else:  # zero - quiet retry unless escalation applies
            state = _project_state(db, ts)
            if state and needs_escalation(state["task_count"], float(state["age_days"])):
                if send_email:
                    send_alert(
                        f"[forms] TS{ts} has tasks flowing but no QA form after "
                        f"{int(float(state['age_days']))} days",
                        f"TECH-OPS: TS{ts} has {state['task_count']:,} asset-task rows but no "
                        f"'ACTIVE - QA Form TS{ts}' exists in Swift yet.\n"
                        f"Discovery retries nightly; this alert repeats until the form "
                        f"appears or a row is inserted manually.",
                    )
            else:
                logger.info(f"TS{ts}: no QA form in Swift yet - will retry nightly")
    return newly
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/Scripts/python.exe -m pytest tests/test_qa_form_discovery.py -v`
Expected: 6 PASS

- [ ] **Step 5: One-shot live smoke of the read-only half (no writes, no email)**

From `swift_api_pipeline/`, run a REPL check that auth + fetch + match work against prod Swift:

```bash
./venv/Scripts/python.exe -c "
from base_extractor import BaseExtractor
class X(BaseExtractor):
    def __init__(self): super().__init__(pipeline_name='qa_form_discovery_smoke')
x = X(); x.authenticate()
from qa_form_discovery import fetch_org_forms, match_qa_form
forms = fetch_org_forms(x.token)
print('forms:', len(forms))
for ts in (19, 20):
    print(ts, match_qa_form(forms, ts))
"
```

Expected: `forms: 49` (±), TS19 → `one` with `-Omun_NWXeQE1tEhSPXf`, TS20 → `zero` (until the form is created in Swift).
NOTE: if `BaseExtractor.__init__` requires more setup than `pipeline_name`, mirror however `FormsExtractor` constructs it.

- [ ] **Step 6: Commit**

```bash
git add swift_api_pipeline/qa_form_discovery.py swift_api_pipeline/tests/test_qa_form_discovery.py
git commit -m "feat(qa-forms): REST auto-discovery + registration + alert emails"
```

---

### Task 9: Wire discovery into the nightly forms pipeline

**Files:**
- Modify: `swift_api_pipeline/main.py` (`run_forms_pipeline`, ~lines 287–297)

**Interfaces:**
- Consumes: `run_discovery(db, token, send_email=True)` (Task 8); `extract_forms.run_forms_pipeline` (Task 7 signature).

- [ ] **Step 1: Add the discovery step**

In `main.py run_forms_pipeline()`, discovery runs first so a newly registered form is included in the same night's extraction. The extractor authenticates inside `extract_forms()`; discovery needs a token *before* that, so it does its own auth via a throwaway `BaseExtractor` (cheap, one POST):

```python
def run_forms_pipeline():
    """Run QA forms extraction + transformation"""
    from extract_forms import run_forms_pipeline as extract_forms
    from transform import run_qa_forms_transform

    # Auto-discovery first: a QA form registered tonight is extracted tonight.
    # Any discovery failure degrades to logs/alert email - never blocks extraction.
    try:
        from base_extractor import BaseExtractor
        from db import get_db
        from qa_form_discovery import run_discovery

        class _DiscoveryAuth(BaseExtractor):
            def __init__(self):
                super().__init__(pipeline_name="qa_form_discovery")

        auth = _DiscoveryAuth()
        auth.authenticate()
        new_ts = run_discovery(get_db(), auth.token)
        if new_ts:
            print(f"QA form auto-discovery registered: {new_ts}")
    except Exception as e:
        print(f"QA form auto-discovery failed (continuing with registered forms): {e}")

    run_id = extract_forms()
    run_qa_forms_transform(run_id)
```

(Match the existing body — keep whatever else `run_forms_pipeline` already does around these calls. If `BaseExtractor` subclassing needs adjustment, mirror `FormsExtractor`.)

- [ ] **Step 2: Compile + full test suite**

Run: `./venv/Scripts/python.exe -m py_compile main.py && ./venv/Scripts/python.exe -m pytest tests/ -q`
Expected: exit 0; same known-failure set, nothing new.

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/main.py
git commit -m "feat(forms): run QA form auto-discovery before nightly extraction"
```

---

### Task 10: Docs, PR, premerge-review, live verification

**Files:**
- Modify: `README.md` (pipelines table: forms row gains auto-discovery mention; exports described as dynamic), `CHANGELOG.md` if present

- [ ] **Step 1: Update README (+CHANGELOG if the repo has one)** — standing rule: README current in the same change as any merge.

- [ ] **Step 2: Push branch, open PR**

```bash
git push -u origin feat/ts-auto-coverage
gh pr create --title "TS project auto-coverage: dynamic exports + ref_qa_forms + QA form auto-discovery" --body "Implements docs/superpowers/specs/2026-08-11-ts-project-auto-coverage-design.md

- Exports (asset-tasks / timer / qa-form) read TS lists from reference.ref_ontel_techops_projects; empty new projects are skipped, not fatal
- config.QA_FORMS replaced by seeded reference.ref_qa_forms (migration 231, RLS)
- Nightly forms pipeline auto-discovers 'ACTIVE - QA Form TS{n}' via GET /api/organizations/{org}/forms (spike-proven), registers it, creates the raw table from a version-controlled template, and emails a confirmation; zero-match retries nightly with 7-day escalation; multi-match asks for a manual pick
- Trigger: TS20 (2026-08-11) blocked the nightly export as a new project

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 3: Run the `premerge-review` skill on the PR** (scope → review lanes → gates → merge). DB preflight lane applies (migration 231 already applied live in Task 2 — reviewer verifies live state matches the file).

- [ ] **Step 4: Post-merge live verification (next nightly cycle)**

- pipeline-forms run: log shows either `no QA form in Swift yet - will retry nightly` for TS20 or a registration line; extraction still processes TS13–19.
- pipeline-asset-tasks-export run: guard prints `TECH-OPS: TS20: SKIPPED (new/empty ...)` and delivers TS13–19 workbooks.
- pipeline-timer run: timer workbooks unchanged for TS13–19.
- When `ACTIVE - QA Form TS20` appears in Swift: confirmation email received, `ref_qa_forms` has the TS20 row with `registered_by='auto-discovery'`, `raw_form_qa_ts20` exists with RLS enabled (`SELECT relrowsecurity FROM pg_class WHERE oid='data_raw.raw_form_qa_ts20'::regclass`).

- [ ] **Step 5: WORK_LOG + Obsidian log entries; memory update** (per session conventions).
