# Calendar Phase 2 Normalization (Wave 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill `leave_type_normalized` and `team_normalized` (currently empty) on `data_staging.stg_calendar_events`, and add canonical person identity, all reference-driven and applied to both the existing 10,857 rows and every future pipeline run.

**Architecture:** New reference tables (`reference.ref_leave_code`, `reference.ref_calendar_team`) plus a pure-function module `calendar_normalize.py` called from the load step. Person is matched to `ref_employees` (deterministic with team-disambiguation, AI-cached tail in `agent.calendar_person_match`); `team_normalized` is derived from the matched employee's `carrier_group` with a label fallback. A `--renormalize` orchestrator mode backfills existing rows in place.

**Tech Stack:** Python 3.12, asyncpg (via `db.py` sync bridge), pytest, Supabase Postgres, Anthropic Haiku (`claude-haiku-4-5-20251001`). Spec: `docs/specs/2026-06-26-calendar-phase2-normalization-design.md`.

## Global Constraints

- Touch only our own schemas: `reference`, `data_staging`, `analytics`, `agent`. Never `public`/`auth`/etc.
- Apply all migrations via the Supabase MCP (`apply_migration`), project `voqfjfngdpcvevbkikud`. The direct DB host needs WARP, which logs Claude out, so MCP is the path. Also commit the `.sql` file under `swift_api_pipeline/migrations/`.
- Every upserted/keyed table has a PRIMARY KEY. `ref_*` naming in `reference`. Follow `DATABASE_ARCHITECTURE.md`.
- Raw columns (`leave_type`, `team`, `person`, `person_note`) are never overwritten; only the `*_normalized` / `emp_id` / `*_source` / `team_level` columns are written.
- Convert any datetime shown to the user to `America/New_York`.
- Run tests from `swift_api_pipeline/`: `venv/Scripts/python -m pytest <file> -v`.
- Haiku model id is exactly `claude-haiku-4-5-20251001`.
- No em-dash in user-facing copy/log strings; use period/comma/colon/parens.

---

## File Structure

- Create `swift_api_pipeline/migrations/126_ref_leave_code.sql` — leave-code reference + seed.
- Create `swift_api_pipeline/migrations/127_ref_calendar_team.sql` — team-label reference + seed.
- Create `swift_api_pipeline/migrations/128_calendar_events_normalize_columns.sql` — new staging columns + person-match cache table.
- Create `swift_api_pipeline/migrations/129_v_calendar_leave_normalized.sql` — expose new columns in the views.
- Create `swift_api_pipeline/calendar_normalize.py` — pure functions: `normalize_leave_type`, `normalize_team`, `build_employee_index`, `match_person_deterministic`.
- Create `swift_api_pipeline/calendar_person_cache.py` — `resolve_person` (cache -> deterministic -> AI) + `extract_person_with_ai`.
- Create `swift_api_pipeline/calendar_lookups.py` — `load_lookups(db)` builds the in-memory maps once per run.
- Modify `swift_api_pipeline/calendar_events_transform.py` — `build_row` accepts a `norm` dict.
- Modify `swift_api_pipeline/calendar_events_load.py` — `load_staging` loads lookups, resolves person/team/leave per row, adds new columns to the upsert.
- Modify `swift_api_pipeline/extract_calendar_events.py` — add `--renormalize` backfill mode.
- Create tests: `test_calendar_normalize.py`, `test_calendar_person_cache.py`; extend `test_calendar_events_transform.py`, `test_calendar_events_load.py`.

---

## Task 1: Migration 126 — `reference.ref_leave_code`

**Files:**
- Create: `swift_api_pipeline/migrations/126_ref_leave_code.sql`
- Verify: via Supabase MCP query

**Interfaces:**
- Produces: table `reference.ref_leave_code(code PK, code_num, label, category, scope_note, requires_rtw_form, is_active, created_at, updated_at)`.

- [ ] **Step 1: Write the migration SQL**

```sql
-- migrations/126_ref_leave_code.sql
-- Leave/work code reference for calendar normalization. Seeded from the
-- authoritative HR daily-report legend (2026-06-26). Unknown legacy codes
-- (LAC/STL/HD/LWOP) are intentionally omitted; they fall back to the raw code.
BEGIN;

CREATE TABLE IF NOT EXISTS reference.ref_leave_code (
    code              text PRIMARY KEY,
    code_num          text,
    label             text,
    category          text,
    scope_note        text,
    requires_rtw_form boolean NOT NULL DEFAULT false,
    is_active         boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

INSERT INTO reference.ref_leave_code (code, code_num, label, category, scope_note, requires_rtw_form) VALUES
    ('RDOT','001','Rest Day Overtime','overtime',NULL,false),
    ('RDO','002','Rest Day Offset','rest',NULL,false),
    ('VL','003','Vacation Leave','leave',NULL,false),
    ('SL','004','Sick Leave','leave',NULL,true),
    ('EL','005','Emergency Leave','leave',NULL,true),
    ('SDL','006','Sudden Leave','leave',NULL,false),
    ('UT','007','Undertime','leave',NULL,false),
    ('BL','008','Birthday Leave','leave',NULL,false),
    ('ML','009','Maternity Leave','leave','start date only',true),
    ('PL','010','Paternity Leave','leave',NULL,false),
    ('SPL','011','Solo Parent Leave','leave',NULL,false),
    ('BRL','013','Bereavement Leave','leave',NULL,false),
    ('LR','015','Weekend Live Review','work','TS Team only',false),
    ('WW','016','Weekend Work','work','TS Team only',false),
    ('LRWD','017','Weekday Live Review','work','TS Team only',false),
    ('LDL','018','Learning & Development Leave','leave',NULL,false),
    ('LDO','019','Learning & Development Overtime','overtime',NULL,false),
    ('RD',NULL,'Rest Day','rest','scheduled rest-day marker',false)
ON CONFLICT (code) DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Apply via Supabase MCP**

Use `apply_migration` with name `126_ref_leave_code` and the body above (without the outer `BEGIN/COMMIT`, since `apply_migration` wraps it in its own transaction). Then commit the `.sql` file.

- [ ] **Step 3: Verify seed**

Run via MCP `execute_sql`:
```sql
SELECT count(*) AS n, count(label) AS labeled FROM reference.ref_leave_code;
SELECT code, label, category, requires_rtw_form FROM reference.ref_leave_code WHERE code IN ('RDO','LR','SL','RD');
```
Expected: `n = 18`, `labeled = 18`; `RDO=Rest Day Offset`, `LR=Weekend Live Review`, `SL` has `requires_rtw_form=true`, `RD=Rest Day`.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/126_ref_leave_code.sql
git commit -m "feat(calendar): add reference.ref_leave_code (migration 126)"
```

---

## Task 2: Migration 127 — `reference.ref_calendar_team`

**Files:**
- Create: `swift_api_pipeline/migrations/127_ref_calendar_team.sql`
- Verify: via Supabase MCP query

**Interfaces:**
- Produces: table `reference.ref_calendar_team(team_raw PK, team_canonical, level, created_at, updated_at)`. Lookup is by `lower(trim(team_raw))` (done in code); one representative row per lowercased key is sufficient.

- [ ] **Step 1: Write the migration SQL**

```sql
-- migrations/127_ref_calendar_team.sql
-- Fallback label->canonical mapping for team_normalized (primary source is the
-- matched employee's carrier_group; this is used for RD/unmatched rows).
-- Canonicals drawn from reference.ref_employees taxonomy. Lookup is by
-- lower(trim(team_raw)); store one row per lowercased key.
BEGIN;

CREATE TABLE IF NOT EXISTS reference.ref_calendar_team (
    team_raw       text PRIMARY KEY,
    team_canonical text,
    level          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

INSERT INTO reference.ref_calendar_team (team_raw, team_canonical, level) VALUES
    ('CG1','CG1 - Verizon','carrier_group'),
    ('CG2','CG2 - AT&T/DISH','carrier_group'),
    ('CG3','CG3 - TMO/USCC','carrier_group'),
    ('Acctg','Accounting','carrier_group'),
    ('Accounting','Accounting','carrier_group'),
    ('Admin and Ops','Admin and Operations','carrier_group'),
    ('Admin & Ops','Admin and Operations','carrier_group'),
    ('T&A','Tools&Auto','carrier_group'),
    ('TNA','Tools&Auto','carrier_group'),
    ('CRTV','Creatives','carrier_group'),
    ('CRTVS','Creatives','carrier_group'),
    ('R&D','Research','carrier_group'),
    ('QPI','QPI','carrier_group'),
    ('DA','DA','carrier_group'),
    ('HR','HR','carrier_group'),
    ('TS Admin','TS-Admin','carrier_group'),
    ('DSM','PHDSM','carrier_group'),
    ('PHIDSM','PHDSM','carrier_group'),
    ('PHIDS','PHDSM','carrier_group'),
    ('PHI DS','PHDSM','carrier_group'),
    ('Swift','Swifttt','carrier_group'),
    ('Alpha','Alpha','cluster'),
    ('Beta','Beta','cluster'),
    ('Gamma','Gamma','cluster'),
    ('Delta','Delta','cluster'),
    ('Epsilon','Epsilon','cluster'),
    ('Zeta','Zeta','cluster'),
    ('MKTG','Marketing','department'),
    ('Marketing','Marketing','department'),
    ('PHI HR','HR','carrier_group'),
    ('T&D','Swifttt','carrier_group'),
    ('Trainee',NULL,'status'),
    ('SD',NULL,'unknown'),
    ('TS Ops',NULL,'unknown')
ON CONFLICT (team_raw) DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Apply via Supabase MCP** (name `127_ref_calendar_team`, body without outer `BEGIN/COMMIT`), then commit the `.sql` file.

- [ ] **Step 3: Verify**

Run via MCP `execute_sql`:
```sql
SELECT lower(team_raw) k, team_canonical, level FROM reference.ref_calendar_team
WHERE lower(team_raw) IN ('cg1','acctg','alpha','phi hr','t&d','trainee');
```
Expected: `cg1 -> CG1 - Verizon`, `acctg -> Accounting`, `alpha -> Alpha (cluster)`, `phi hr -> HR`, `t&d -> Swifttt`, `trainee -> (null) status`.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/127_ref_calendar_team.sql
git commit -m "feat(calendar): add reference.ref_calendar_team (migration 127)"
```

---

## Task 3: Migration 128 — staging columns + person-match cache

**Files:**
- Create: `swift_api_pipeline/migrations/128_calendar_events_normalize_columns.sql`
- Verify: via Supabase MCP query

**Interfaces:**
- Produces: new nullable columns on `data_staging.stg_calendar_events`: `person_normalized text`, `emp_id text`, `person_match_source text`, `team_level text`, `person_note_normalized text`. New table `agent.calendar_person_match(person_raw, team_raw, emp_id, person_normalized, confidence, match_source, resolved_at, PK(person_raw, team_raw))`.

- [ ] **Step 1: Write the migration SQL**

```sql
-- migrations/128_calendar_events_normalize_columns.sql
BEGIN;

ALTER TABLE data_staging.stg_calendar_events
    ADD COLUMN IF NOT EXISTS person_normalized      text,
    ADD COLUMN IF NOT EXISTS emp_id                 text,
    ADD COLUMN IF NOT EXISTS person_match_source    text,
    ADD COLUMN IF NOT EXISTS team_level             text,
    ADD COLUMN IF NOT EXISTS person_note_normalized text;

CREATE TABLE IF NOT EXISTS agent.calendar_person_match (
    person_raw        text NOT NULL,
    team_raw          text NOT NULL DEFAULT '',
    emp_id            text,
    person_normalized text,
    confidence        numeric,
    match_source      text,
    resolved_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (person_raw, team_raw)
);

COMMIT;
```

- [ ] **Step 2: Apply via Supabase MCP** (name `128_calendar_events_normalize_columns`, body without outer `BEGIN/COMMIT`), then commit the `.sql` file.

- [ ] **Step 3: Verify**

Run via MCP `execute_sql`:
```sql
SELECT column_name FROM information_schema.columns
WHERE table_schema='data_staging' AND table_name='stg_calendar_events'
  AND column_name IN ('person_normalized','emp_id','person_match_source','team_level','person_note_normalized')
ORDER BY column_name;
SELECT count(*) FROM agent.calendar_person_match;
```
Expected: all 5 columns listed; cache table exists (count 0).

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/128_calendar_events_normalize_columns.sql
git commit -m "feat(calendar): add normalize columns + person-match cache (migration 128)"
```

---

## Task 4: Migration 129 — expose new columns in views

**Files:**
- Create: `swift_api_pipeline/migrations/129_v_calendar_leave_normalized.sql`
- Verify: via Supabase MCP query

**Interfaces:**
- Consumes: columns added in Task 3.
- Produces: `analytics.v_calendar_leave` and `v_calendar_leave_daily` now expose `person_normalized`, `emp_id`, `person_match_source`, `team_level`, `person_note_normalized` (in addition to the existing `leave_type_normalized` / `team_normalized`).

- [ ] **Step 1: Write the migration SQL** (recreates the views from migration 125, adding the new columns)

```sql
-- migrations/129_v_calendar_leave_normalized.sql
BEGIN;

DROP VIEW IF EXISTS analytics.v_calendar_leave_daily;
DROP VIEW IF EXISTS analytics.v_calendar_leave;

CREATE VIEW analytics.v_calendar_leave AS
SELECT event_id, summary_raw AS summary, leave_type, leave_type_normalized,
       team, team_normalized, team_level, person, person_normalized, emp_id,
       person_match_source, person_note, person_note_normalized, rest_day_of_week,
       start_date, end_date, days, is_all_day, creator_email,
       event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'leave' AND NOT is_deleted;

CREATE VIEW analytics.v_calendar_leave_daily AS
SELECT e.*, gs::date AS leave_date
FROM analytics.v_calendar_leave e
CROSS JOIN LATERAL generate_series(e.start_date, e.end_date, interval '1 day') gs;

COMMIT;
```

- [ ] **Step 2: Apply via Supabase MCP** (name `129_v_calendar_leave_normalized`, body without outer `BEGIN/COMMIT`), then commit the `.sql` file.

- [ ] **Step 3: Verify**

Run via MCP `execute_sql`:
```sql
SELECT column_name FROM information_schema.columns
WHERE table_schema='analytics' AND table_name='v_calendar_leave'
  AND column_name IN ('team_level','person_normalized','emp_id','person_note_normalized')
ORDER BY column_name;
```
Expected: all 4 present.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/129_v_calendar_leave_normalized.sql
git commit -m "feat(calendar): expose normalize columns in calendar views (migration 129)"
```

---

## Task 5: `normalize_leave_type` (pure)

**Files:**
- Create: `swift_api_pipeline/calendar_normalize.py`
- Test: `swift_api_pipeline/test_calendar_normalize.py`

**Interfaces:**
- Produces: `normalize_leave_type(code: str | None, code_map: dict) -> tuple[str | None, str | None]`. `code_map` maps `UPPERCODE -> (label, category)`. Returns `(leave_type_normalized, category)`. Unknown code falls back to the raw code, category None. Compound codes (`UT/SL`) join part labels with ` + ` and category `compound`.

- [ ] **Step 1: Write the failing test**

```python
# test_calendar_normalize.py
"""Unit tests for calendar normalization pure functions. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_normalize.py -v
"""
from calendar_normalize import normalize_leave_type

CODE_MAP = {
    "VL": ("Vacation Leave", "leave"),
    "SL": ("Sick Leave", "leave"),
    "UT": ("Undertime", "leave"),
    "RD": ("Rest Day", "rest"),
}


def test_leave_type_known():
    assert normalize_leave_type("VL", CODE_MAP) == ("Vacation Leave", "leave")


def test_leave_type_case_insensitive():
    assert normalize_leave_type("ww", {"WW": ("Weekend Work", "work")}) == ("Weekend Work", "work")


def test_leave_type_unknown_falls_back_to_raw():
    assert normalize_leave_type("LAC", CODE_MAP) == ("LAC", None)


def test_leave_type_compound():
    label, cat = normalize_leave_type("UT/SL", CODE_MAP)
    assert label == "Undertime + Sick Leave"
    assert cat == "compound"


def test_leave_type_compound_with_spaces():
    label, cat = normalize_leave_type("VL / LAC", CODE_MAP)
    assert label == "Vacation Leave + LAC"
    assert cat == "compound"


def test_leave_type_none():
    assert normalize_leave_type(None, CODE_MAP) == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError: cannot import name 'normalize_leave_type'`.

- [ ] **Step 3: Write minimal implementation**

```python
# calendar_normalize.py
"""Pure normalization functions for calendar events. No DB, no network: all
lookups are passed in, so these are unit-testable in isolation."""
import re


def normalize_leave_type(code, code_map):
    """Map a leave code to (label, category). Case-insensitive. Compound codes
    (e.g. 'UT/SL') join part labels with ' + ' and category 'compound'. Unknown
    codes fall back to the raw code with category None."""
    if not code or not code.strip():
        return None, None
    raw = code.strip()
    key = raw.upper().replace(" ", "")
    if "/" in key:
        parts = [p for p in key.split("/") if p]
        labels = [(code_map.get(p, (None, None))[0] or p) for p in parts]
        return " + ".join(labels), "compound"
    label, category = code_map.get(key, (None, None))
    return (label or raw), category
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_normalize.py swift_api_pipeline/test_calendar_normalize.py
git commit -m "feat(calendar): normalize_leave_type pure function"
```

---

## Task 6: `normalize_team` (pure, person-derived with label fallback)

**Files:**
- Modify: `swift_api_pipeline/calendar_normalize.py`
- Test: `swift_api_pipeline/test_calendar_normalize.py`

**Interfaces:**
- Produces: `normalize_team(emp: dict | None, team_raw: str | None, team_map: dict) -> tuple[str | None, str | None]`. `team_map` maps `lower(team_raw) -> (team_canonical, level)`. Returns `(team_normalized, team_level)`. When `emp` is truthy and has `carrier_group`, returns that with level `carrier_group`; else falls back to `team_map`; else `(None, None)`.

- [ ] **Step 1: Write the failing test** (append to `test_calendar_normalize.py`)

```python
from calendar_normalize import normalize_team

TEAM_MAP = {
    "cg1": ("CG1 - Verizon", "carrier_group"),
    "alpha": ("Alpha", "cluster"),
    "trainee": (None, "status"),
}


def test_team_person_derived_uses_carrier_group():
    emp = {"carrier_group": "CG2 - AT&T/DISH", "cluster": "Epsilon"}
    assert normalize_team(emp, "Trainee", TEAM_MAP) == ("CG2 - AT&T/DISH", "carrier_group")


def test_team_fallback_to_label_when_no_emp():
    assert normalize_team(None, "CG1", TEAM_MAP) == ("CG1 - Verizon", "carrier_group")


def test_team_fallback_label_cluster_for_rd_row():
    assert normalize_team(None, "Alpha", TEAM_MAP) == ("Alpha", "cluster")


def test_team_unmapped_label_is_null():
    assert normalize_team(None, "Trainee", TEAM_MAP) == (None, "status")
    assert normalize_team(None, "Nonsense", TEAM_MAP) == (None, None)


def test_team_no_emp_no_team():
    assert normalize_team(None, None, TEAM_MAP) == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -k team -v`
Expected: FAIL with `ImportError: cannot import name 'normalize_team'`.

- [ ] **Step 3: Add implementation to `calendar_normalize.py`**

```python
def normalize_team(emp, team_raw, team_map):
    """Person-derived team with label fallback. If a matched employee is given,
    use their carrier_group. Otherwise fall back to the cleaned label map
    (RD rest-day rows, unmatched people). (None, None) when neither applies."""
    if emp and emp.get("carrier_group"):
        return emp["carrier_group"], "carrier_group"
    if team_raw and team_raw.strip():
        return team_map.get(team_raw.strip().lower(), (None, None))
    return None, None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_normalize.py swift_api_pipeline/test_calendar_normalize.py
git commit -m "feat(calendar): normalize_team person-derived with label fallback"
```

---

## Task 7: Employee index + deterministic person match (pure)

**Files:**
- Modify: `swift_api_pipeline/calendar_normalize.py`
- Test: `swift_api_pipeline/test_calendar_normalize.py`

**Interfaces:**
- Produces:
  - `build_employee_index(emp_rows: list[dict]) -> dict[str, list[dict]]`: maps `lower(name)` to candidate employees, indexing `nickname`, `first_name`, `full_name`.
  - `match_person_deterministic(person_raw, team_raw, emp_index, team_map) -> tuple[dict | None, str]`: returns `(emp, source)` with source in `exact` / `ambiguous` / `unmatched`. Multiple name matches are disambiguated by the row's team (candidate `carrier_group` or `cluster` equals the team label's canonical).

- [ ] **Step 1: Write the failing test** (append to `test_calendar_normalize.py`)

```python
from calendar_normalize import build_employee_index, match_person_deterministic

EMPLOYEES = [
    {"emp_id": "E1", "full_name": "Edward Cruz", "first_name": "Edward", "nickname": "Ed",
     "carrier_group": "CG1 - Verizon", "cluster": "Alpha"},
    {"emp_id": "E2", "full_name": "Edwin Santos", "first_name": "Edwin", "nickname": "Ed",
     "carrier_group": "CG2 - AT&T/DISH", "cluster": "Epsilon"},
    {"emp_id": "E3", "full_name": "Prince Uy", "first_name": "Prince", "nickname": None,
     "carrier_group": "Creatives", "cluster": None},
]
EMP_INDEX = build_employee_index(EMPLOYEES)
TEAM_MAP2 = {"cg1": ("CG1 - Verizon", "carrier_group"), "cg2": ("CG2 - AT&T/DISH", "carrier_group"),
             "crtv": ("Creatives", "carrier_group")}


def test_index_keys_lowercased():
    assert "ed" in EMP_INDEX and "prince" in EMP_INDEX and "edward cruz" in EMP_INDEX


def test_match_unique_first_name():
    emp, src = match_person_deterministic("Prince", "CRTV", EMP_INDEX, TEAM_MAP2)
    assert src == "exact" and emp["emp_id"] == "E3"


def test_match_ambiguous_nickname_disambiguated_by_team():
    emp, src = match_person_deterministic("Ed", "CG2", EMP_INDEX, TEAM_MAP2)
    assert src == "exact" and emp["emp_id"] == "E2"


def test_match_ambiguous_without_team_signal():
    emp, src = match_person_deterministic("Ed", "Nonsense", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "ambiguous"


def test_match_unmatched():
    emp, src = match_person_deterministic("Zzz", "CG1", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "unmatched"


def test_match_none_person():
    emp, src = match_person_deterministic(None, "CG1", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "unmatched"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -k "index or match" -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add implementation to `calendar_normalize.py`**

```python
def build_employee_index(emp_rows):
    """Map lower(name) -> list of employee dicts, indexing nickname, first_name,
    and full_name. Include resigned employees (historical leave references them)."""
    idx = {}
    for e in emp_rows:
        for name in (e.get("nickname"), e.get("first_name"), e.get("full_name")):
            if name and name.strip():
                idx.setdefault(name.strip().lower(), []).append(e)
    return idx


def match_person_deterministic(person_raw, team_raw, emp_index, team_map):
    """Match a calendar person to an employee. Returns (emp, source) where source
    is exact / ambiguous / unmatched. Multiple name matches are disambiguated by
    the row's team (candidate carrier_group or cluster equals the team canonical)."""
    if not person_raw or not person_raw.strip():
        return None, "unmatched"
    cands = emp_index.get(person_raw.strip().lower(), [])
    if not cands:
        return None, "unmatched"
    if len(cands) == 1:
        return cands[0], "exact"
    canon, _level = team_map.get((team_raw or "").strip().lower(), (None, None))
    if canon:
        for c in cands:
            if c.get("carrier_group") == canon or c.get("cluster") == canon:
                return c, "exact"
    return None, "ambiguous"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/Scripts/python -m pytest test_calendar_normalize.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_normalize.py swift_api_pipeline/test_calendar_normalize.py
git commit -m "feat(calendar): employee index + deterministic person matching"
```

---

## Task 8: Person resolver with AI fallback + cache

**Files:**
- Create: `swift_api_pipeline/calendar_person_cache.py`
- Test: `swift_api_pipeline/test_calendar_person_cache.py`

**Interfaces:**
- Consumes: `build_employee_index`, `match_person_deterministic` (Task 7); `db` with `.fetchrow(query, *args)` and `.execute(query, *args)` (see `FakeDB` pattern).
- Produces: `resolve_person(db, person_raw, team_raw, emp_index, team_map, ai_fn=extract_person_with_ai) -> dict` returning keys `emp_id`, `person_normalized`, `confidence`, `match_source`. Cache table `agent.calendar_person_match` keyed on `(person_raw, team_raw)`. Also `extract_person_with_ai(person_raw, team_raw, candidate_names) -> dict | None`.

- [ ] **Step 1: Write the failing test**

```python
# test_calendar_person_cache.py
"""Unit tests for the person resolver using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_person_cache.py -v
"""
from calendar_person_cache import resolve_person
from calendar_normalize import build_employee_index

EMPLOYEES = [
    {"emp_id": "E1", "full_name": "Edward Cruz", "first_name": "Edward", "nickname": "Ed",
     "carrier_group": "CG1 - Verizon", "cluster": "Alpha"},
    {"emp_id": "E3", "full_name": "Prince Uy", "first_name": "Prince", "nickname": None,
     "carrier_group": "Creatives", "cluster": None},
]
EMP_INDEX = build_employee_index(EMPLOYEES)
TEAM_MAP = {"crtv": ("Creatives", "carrier_group"), "cg1": ("CG1 - Verizon", "carrier_group")}


class FakeDB:
    def __init__(self):
        self.rows = {}      # (person_raw, team_raw) -> record
        self.writes = 0

    def fetchrow(self, query, *args):
        return self.rows.get((args[0], args[1]))

    def execute(self, query, *args):
        self.writes += 1
        self.rows[(args[0], args[1])] = {
            "person_raw": args[0], "team_raw": args[1], "emp_id": args[2],
            "person_normalized": args[3], "confidence": args[4], "match_source": args[5],
        }
        return "INSERT 0 1"


def test_resolve_exact_no_ai_and_caches():
    db = FakeDB()
    calls = {"n": 0}
    def ai_fn(*a, **k):
        calls["n"] += 1
        return None
    r = resolve_person(db, "Prince", "CRTV", EMP_INDEX, TEAM_MAP, ai_fn=ai_fn)
    assert r["emp_id"] == "E3" and r["person_normalized"] == "Prince Uy"
    assert r["match_source"] == "exact"
    assert calls["n"] == 0 and db.writes == 1


def test_resolve_cache_hit_skips_match():
    db = FakeDB()
    db.rows[("Prince", "CRTV")] = {"person_raw": "Prince", "team_raw": "CRTV",
        "emp_id": "E3", "person_normalized": "Prince Uy", "confidence": 1.0,
        "match_source": "exact"}
    r = resolve_person(db, "Prince", "CRTV", EMP_INDEX, TEAM_MAP)
    assert r["emp_id"] == "E3" and db.writes == 0


def test_resolve_unmatched_calls_ai_then_caches():
    db = FakeDB()
    def ai_fn(person_raw, team_raw, candidate_names):
        return {"emp_id": "E1", "person_normalized": "Edward Cruz", "confidence": 0.7}
    r = resolve_person(db, "Eddie", "CG1", EMP_INDEX, TEAM_MAP, ai_fn=ai_fn)
    assert r["emp_id"] == "E1" and r["match_source"] == "ai"
    assert db.writes == 1


def test_resolve_ai_gives_up_marks_unmatched():
    db = FakeDB()
    r = resolve_person(db, "Ghost", "CG1", EMP_INDEX, TEAM_MAP, ai_fn=lambda *a, **k: None)
    assert r["emp_id"] is None and r["match_source"] == "unmatched"
    assert db.writes == 1


def test_resolve_null_person_no_write():
    db = FakeDB()
    r = resolve_person(db, None, "CG1", EMP_INDEX, TEAM_MAP)
    assert r["emp_id"] is None and r["match_source"] == "unmatched"
    assert db.writes == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python -m pytest test_calendar_person_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'calendar_person_cache'`.

- [ ] **Step 3: Write the implementation**

```python
# calendar_person_cache.py
"""Resolve a calendar person to a canonical employee, caching the result in
agent.calendar_person_match keyed on (person_raw, team_raw). Cache -> deterministic
match -> Haiku fallback for the tail. Mirrors calendar_parse_cache."""
import os
import re
import json

from calendar_normalize import match_person_deterministic

MODEL = "claude-haiku-4-5-20251001"


def _unmatched():
    return {"emp_id": None, "person_normalized": None, "confidence": 0.0,
            "match_source": "unmatched"}


def _write_cache(db, person_raw, team_raw, result):
    db.execute(
        "INSERT INTO agent.calendar_person_match "
        "(person_raw, team_raw, emp_id, person_normalized, confidence, match_source) "
        "VALUES ($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT (person_raw, team_raw) DO UPDATE SET "
        "  emp_id=EXCLUDED.emp_id, person_normalized=EXCLUDED.person_normalized, "
        "  confidence=EXCLUDED.confidence, match_source=EXCLUDED.match_source, "
        "  resolved_at=now()",
        person_raw, team_raw, result["emp_id"], result["person_normalized"],
        result["confidence"], result["match_source"],
    )


def extract_person_with_ai(person_raw, team_raw, candidate_names):
    """Ask Haiku which employee (from candidate_names) the calendar name refers to.
    Returns {emp_id?, person_normalized, confidence} or None. Never raises."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        prompt = (
            "Match a name from a shared work calendar to the correct employee.\n"
            f"Calendar name: {json.dumps(person_raw)}\n"
            f"Team on the entry: {json.dumps(team_raw)}\n"
            f"Candidate full names: {json.dumps(candidate_names)}\n\n"
            "Return ONLY a JSON object: {\"person_normalized\": <one candidate full name "
            "or null>, \"confidence\": <0..1>}. Use null if no candidate is a confident match."
        )
        resp = client.messages.create(model=MODEL, max_tokens=128,
                                      messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        d = json.loads(m.group(0))
        name = d.get("person_normalized")
        if not name:
            return None
        return {"person_normalized": name, "confidence": float(d.get("confidence") or 0.5)}
    except Exception:
        return None


def resolve_person(db, person_raw, team_raw, emp_index, team_map, ai_fn=extract_person_with_ai):
    """Cache -> deterministic -> AI. Returns dict with emp_id, person_normalized,
    confidence, match_source. NULL person is unmatched and not cached."""
    if not person_raw or not person_raw.strip():
        return _unmatched()
    tkey = team_raw or ""
    cached = db.fetchrow(
        "SELECT emp_id, person_normalized, confidence, match_source "
        "FROM agent.calendar_person_match WHERE person_raw=$1 AND team_raw=$2",
        person_raw, tkey)
    if cached is not None:
        return {"emp_id": cached["emp_id"], "person_normalized": cached["person_normalized"],
                "confidence": cached["confidence"], "match_source": cached["match_source"]}

    emp, source = match_person_deterministic(person_raw, team_raw, emp_index, team_map)
    if emp is not None:
        result = {"emp_id": emp.get("emp_id"), "person_normalized": emp.get("full_name"),
                  "confidence": 1.0, "match_source": "exact"}
    else:
        candidate_names = sorted({e.get("full_name") for e in
                                  sum(emp_index.values(), []) if e.get("full_name")})
        ai = ai_fn(person_raw, team_raw, candidate_names)
        if ai and ai.get("person_normalized"):
            # resolve the AI-chosen full name back to an emp_id via the index
            chosen = ai["person_normalized"]
            matches = emp_index.get(chosen.strip().lower(), [])
            emp_id = matches[0].get("emp_id") if matches else None
            result = {"emp_id": emp_id, "person_normalized": chosen,
                      "confidence": ai.get("confidence", 0.5), "match_source": "ai"}
        else:
            result = _unmatched()

    _write_cache(db, person_raw, tkey, result)
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/Scripts/python -m pytest test_calendar_person_cache.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_person_cache.py swift_api_pipeline/test_calendar_person_cache.py
git commit -m "feat(calendar): person resolver with AI fallback + cache"
```

---

## Task 9: Lookups loader + transform/load integration

**Files:**
- Create: `swift_api_pipeline/calendar_lookups.py`
- Modify: `swift_api_pipeline/calendar_events_transform.py` (build_row signature)
- Modify: `swift_api_pipeline/calendar_events_load.py` (load_staging + upsert cols)
- Test: `swift_api_pipeline/test_calendar_events_transform.py`

**Interfaces:**
- Consumes: `normalize_leave_type`, `normalize_team` (Tasks 5/6); `build_employee_index` (Task 7); `resolve_person` (Task 8).
- Produces:
  - `calendar_lookups.load_lookups(db) -> dict` with keys `code_map`, `team_map`, `emp_index`.
  - `build_row(ev, shape, run_id, norm)` where `norm` is a dict with keys `leave_type_normalized`, `team_normalized`, `team_level`, `person_normalized`, `emp_id`, `person_match_source`, `person_note_normalized`.

- [ ] **Step 1: Write the failing test for `build_row` (append to `test_calendar_events_transform.py`)**

```python
from calendar_events_transform import build_row


def test_build_row_includes_normalized_fields():
    ev = {"id": "x1", "summary": "VL - CG1 - Ed",
          "start": {"date": "2026-06-26"}, "end": {"date": "2026-06-27"}}
    shape = {"event_kind": "leave", "leave_type": "VL", "team": "CG1", "person": "Ed",
             "person_note": None, "rest_day_of_week": None, "parse_source": "deterministic",
             "confidence": 0.95, "needs_review": False}
    norm = {"leave_type_normalized": "Vacation Leave", "team_normalized": "CG1 - Verizon",
            "team_level": "carrier_group", "person_normalized": "Edward Cruz", "emp_id": "E1",
            "person_match_source": "exact", "person_note_normalized": None}
    row = build_row(ev, shape, "run1", norm)
    assert row["leave_type_normalized"] == "Vacation Leave"
    assert row["team_normalized"] == "CG1 - Verizon"
    assert row["team_level"] == "carrier_group"
    assert row["person_normalized"] == "Edward Cruz"
    assert row["emp_id"] == "E1"
    assert row["person_match_source"] == "exact"
    assert row["leave_type"] == "VL"      # raw preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python -m pytest test_calendar_events_transform.py -k normalized -v`
Expected: FAIL (`build_row() missing 1 required positional argument: 'norm'` or KeyError).

- [ ] **Step 3: Modify `build_row` in `calendar_events_transform.py`**

Replace the signature and the two Phase-2 placeholder lines plus add the new keys:

```python
def build_row(ev: dict, shape: dict, run_id: str, norm: dict) -> dict:
    summary = (ev.get("summary") or "").strip() or None
    start_date, end_date, days, is_all_day = event_dates(ev)
    return {
        "event_id": ev.get("id", ""),
        "ical_uid": ev.get("iCalUID"),
        "summary_raw": summary,
        "event_kind": shape["event_kind"],
        "leave_type": shape["leave_type"],
        "leave_type_normalized": norm["leave_type_normalized"],
        "team": shape["team"],
        "team_normalized": norm["team_normalized"],
        "team_level": norm["team_level"],
        "person": shape["person"],
        "person_normalized": norm["person_normalized"],
        "emp_id": norm["emp_id"],
        "person_match_source": norm["person_match_source"],
        "person_note": shape["person_note"],
        "person_note_normalized": norm["person_note_normalized"],
        "rest_day_of_week": shape["rest_day_of_week"],
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "is_all_day": is_all_day,
        "creator_email": (ev.get("creator") or {}).get("email"),
        "event_created": _ts(ev.get("created")),
        "event_updated": _ts(ev.get("updated")),
        "parse_source": shape["parse_source"],
        "parse_confidence": shape["confidence"],
        "needs_review": shape["needs_review"],
        "is_deleted": False,
        "run_id": run_id,
    }
```

- [ ] **Step 4: Run the build_row test to verify it passes**

Run: `venv/Scripts/python -m pytest test_calendar_events_transform.py -k normalized -v`
Expected: PASS.

- [ ] **Step 5: Write `calendar_lookups.py`**

```python
# calendar_lookups.py
"""Load the reference maps + employee index once per run."""
from calendar_normalize import build_employee_index


def load_lookups(db):
    code_rows = db.fetch("SELECT code, label, category FROM reference.ref_leave_code")
    code_map = {r["code"].upper(): (r["label"], r["category"]) for r in code_rows}

    team_rows = db.fetch("SELECT team_raw, team_canonical, level FROM reference.ref_calendar_team")
    team_map = {r["team_raw"].strip().lower(): (r["team_canonical"], r["level"]) for r in team_rows}

    emp_rows = db.fetch(
        "SELECT emp_id, full_name, first_name, nickname, carrier_group, cluster "
        "FROM reference.ref_employees")
    emps = [dict(r) for r in emp_rows]
    emp_index = build_employee_index(emps)
    emp_by_id = {e["emp_id"]: e for e in emps if e.get("emp_id")}

    return {"code_map": code_map, "team_map": team_map,
            "emp_index": emp_index, "emp_by_id": emp_by_id}
```

- [ ] **Step 6: Wire normalization into `load_staging` in `calendar_events_load.py`**

Add the new columns to `_UPSERT_COLS` (after the matching raw columns):

```python
_UPSERT_COLS = [
    "event_id", "ical_uid", "summary_raw", "event_kind", "leave_type",
    "leave_type_normalized", "team", "team_normalized", "team_level",
    "person", "person_normalized", "emp_id", "person_match_source",
    "person_note", "person_note_normalized", "rest_day_of_week", "start_date",
    "end_date", "days", "is_all_day", "creator_email", "event_created",
    "event_updated", "parse_source", "parse_confidence", "needs_review",
    "is_deleted", "run_id",
]
```

Add imports and a per-row enrichment helper at the top of the module:

```python
from calendar_lookups import load_lookups
from calendar_normalize import normalize_leave_type, normalize_team
from calendar_person_cache import resolve_person


def _enrich(db, shape, lookups):
    lt_norm, _cat = normalize_leave_type(shape.get("leave_type"), lookups["code_map"])
    pm = resolve_person(db, shape.get("person"), shape.get("team"),
                        lookups["emp_index"], lookups["team_map"])
    emp = lookups["emp_by_id"].get(pm["emp_id"]) if pm["emp_id"] else None
    team_norm, team_level = normalize_team(emp, shape.get("team"), lookups["team_map"])
    return {
        "leave_type_normalized": lt_norm,
        "team_normalized": team_norm,
        "team_level": team_level,
        "person_normalized": pm["person_normalized"],
        "emp_id": pm["emp_id"],
        "person_match_source": pm["match_source"],
        "person_note_normalized": None,   # filled in Wave 2
    }
```

Then in `load_staging`, load lookups once and pass `norm` to `build_row`:

```python
def load_staging(db, run_id: str, events: list, resolve_fn) -> dict:
    upserted = tombstoned = skipped = 0
    rows = []
    cancelled_ids = []
    lookups = load_lookups(db)
    for ev in events:
        if ev.get("status") == "cancelled":
            eid = ev.get("id", "")
            if eid:
                cancelled_ids.append(eid)
            continue
        try:
            shape = resolve_fn(db, ev.get("summary") or "")
            norm = _enrich(db, shape, lookups)
            rows.append(build_row(ev, shape, run_id, norm))
        except Exception as e:
            skipped += 1
            logger.warning(f"  Skipped event {ev.get('id','?')}: {e}")
    # ... unchanged from here (tombstone, batched upsert) ...
```

(The tombstone + batched-upsert portion below the loop is unchanged.)

- [ ] **Step 7: Run the load + transform tests to verify they pass**

Run: `venv/Scripts/python -m pytest test_calendar_events_transform.py test_calendar_events_load.py -v`
Expected: PASS. If `test_calendar_events_load.py` constructs rows with the old `build_row` arity or asserts the old `_UPSERT_COLS`, update those expectations to include the new columns (pass a `norm` dict of Nones where a leave context is not under test), then re-run.

- [ ] **Step 8: Commit**

```bash
git add swift_api_pipeline/calendar_lookups.py swift_api_pipeline/calendar_events_transform.py \
        swift_api_pipeline/calendar_events_load.py swift_api_pipeline/test_calendar_events_transform.py \
        swift_api_pipeline/test_calendar_events_load.py
git commit -m "feat(calendar): wire leave/team/person normalization into load_staging"
```

---

## Task 10: `--renormalize` backfill mode + run

**Files:**
- Modify: `swift_api_pipeline/extract_calendar_events.py`
- Verify: via Supabase MCP query

**Interfaces:**
- Consumes: `load_lookups`, `_enrich` logic (Task 9). Re-enriches existing staging rows in place using their already-stored raw `leave_type` / `team` / `person`, with no Calendar fetch.
- Produces: `python extract_calendar_events.py --renormalize` updates `leave_type_normalized`, `team_normalized`, `team_level`, `person_normalized`, `emp_id`, `person_match_source` for all existing rows.

- [ ] **Step 1: Add a `renormalize` function and CLI flag to `extract_calendar_events.py`**

```python
def renormalize(db):
    """Re-enrich existing stg_calendar_events rows in place (no Calendar fetch)."""
    from calendar_lookups import load_lookups
    from calendar_normalize import normalize_leave_type, normalize_team
    from calendar_person_cache import resolve_person

    lookups = load_lookups(db)
    rows = db.fetch(
        "SELECT event_id, leave_type, team, person FROM data_staging.stg_calendar_events")
    logger.info(f"Renormalizing {len(rows)} rows")
    updates = []
    for r in rows:
        lt_norm, _cat = normalize_leave_type(r["leave_type"], lookups["code_map"])
        pm = resolve_person(db, r["person"], r["team"], lookups["emp_index"], lookups["team_map"])
        emp = lookups["emp_by_id"].get(pm["emp_id"]) if pm["emp_id"] else None
        team_norm, team_level = normalize_team(emp, r["team"], lookups["team_map"])
        updates.append((r["event_id"], lt_norm, team_norm, team_level,
                        pm["person_normalized"], pm["emp_id"], pm["match_source"]))

    sql = (
        "UPDATE data_staging.stg_calendar_events AS s SET "
        "  leave_type_normalized = v.ltn, team_normalized = v.tn, team_level = v.tl, "
        "  person_normalized = v.pn, emp_id = v.eid, person_match_source = v.pms "
        "FROM (VALUES ($1,$2,$3,$4,$5,$6,$7)) AS v(event_id, ltn, tn, tl, pn, eid, pms) "
        "WHERE s.event_id = v.event_id"
    )
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        retry_db(lambda b=batch: db.executemany(sql, b),
                 description=f"renormalize batch {i//500+1}")
    logger.info(f"Renormalized {len(updates)} rows")
```

Update `main()` and the argument parser:

```python
def main(full_refresh: bool = False, renorm: bool = False):
    db = get_db()
    if renorm:
        renormalize(db)
        close_db()
        return
    # ... existing fetch/load body unchanged ...


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-refresh", action="store_true")
    ap.add_argument("--renormalize", action="store_true")
    a = ap.parse_args()
    main(full_refresh=a.full_refresh, renorm=a.renormalize)
```

Add `retry_db` to the existing `from config import ...` line if not already imported.

- [ ] **Step 2: Run the existing test suite to confirm no regressions**

Run: `venv/Scripts/python -m pytest test_calendar_parse.py test_calendar_parse_cache.py test_calendar_normalize.py test_calendar_person_cache.py test_calendar_events_transform.py test_calendar_events_load.py -v`
Expected: PASS (all).

- [ ] **Step 3: Execute the backfill**

Run the backfill via GHA (preferred, reaches the DB without WARP) or, if running locally with WARP on, `venv/Scripts/python extract_calendar_events.py --renormalize`. Capture the log line "Renormalized N rows" (expect N = current `stg_calendar_events` row count).

- [ ] **Step 4: Verify data quality via Supabase MCP**

Run via MCP `execute_sql`:
```sql
SELECT
  count(*) FILTER (WHERE team IS NOT NULL AND team_normalized IS NULL) AS team_unmapped,
  count(*) FILTER (WHERE leave_type IS NOT NULL AND leave_type_normalized IS NULL) AS lt_unmapped,
  count(*) FILTER (WHERE person_match_source = 'exact') AS person_exact,
  count(*) FILTER (WHERE person_match_source = 'ai') AS person_ai,
  count(*) FILTER (WHERE person IS NOT NULL AND person_match_source = 'unmatched') AS person_unmatched
FROM data_staging.stg_calendar_events;

-- Any team labels still unmapped (should only be SD / TS Ops / typos):
SELECT team, count(*) FROM data_staging.stg_calendar_events
WHERE team IS NOT NULL AND team_normalized IS NULL GROUP BY team ORDER BY 2 DESC;
```
Expected: `lt_unmapped = 0` (all known/legacy codes resolved or fall back to raw, so normalized is never NULL where leave_type is set); `team_unmapped` limited to `SD`/`TS Ops`/unmatched-person edge cases; person match distribution reported. Convert any timestamps shown to ET if surfaced to the user.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/extract_calendar_events.py
git commit -m "feat(calendar): --renormalize backfill mode for normalize columns"
```

---

## Notes on out-of-scope (Wave 2)

`person_note_normalized` is added to the schema (Task 3) and threaded through as `None`
(Task 9) but is filled in Wave 2 (a separate plan): a `normalize_person_note` regex plus a
backfill. The `--renormalize` mode there will extend the UPDATE to set it.

## Self-review notes

- Spec coverage: leave_type (Tasks 1, 5, 9, 10), team person-derived + fallback (Tasks 2,
  6, 7, 9, 10), person matching + AI cache (Tasks 3, 7, 8, 9, 10), schema/views (Tasks 3,
  4), backfill (Task 10), testing (Tasks 5-9). person_note is explicitly deferred to Wave 2.
- Type consistency: `norm` dict keys match between `build_row` (Task 9), `_enrich` (Task 9),
  and `renormalize` (Task 10). `resolve_person` return keys (`emp_id`, `person_normalized`,
  `confidence`, `match_source`) are consumed consistently. `_UPSERT_COLS` matches `build_row`
  keys.
