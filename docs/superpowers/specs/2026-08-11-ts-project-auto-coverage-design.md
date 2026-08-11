# TS Project Auto-Coverage — Design

**Date:** 2026-08-11
**Trigger:** TECH-OPS: TS20 appeared 2026-08-11 with 0 tasks and blocked the nightly
asset-tasks export (abnormal-count guard note). The guard rule itself was fixed the same
night (`9168639`: no-baseline projects never alarm). This design closes the remaining gap:
every system that consumes a TS project list must pick up new TS projects automatically.

## Inventory (verified 2026-08-11)

Already dynamic via `reference.ref_ontel_techops_projects` (view over `stg_projects`,
derives `project_number` from the name; picks up new TS the moment the projects pipeline
lands them): `extract_asset_tasks.py` (≥13, auto-creates partitions), `extract_assets.py`,
`extract_timer.py`, `extract_requirements.py`, `transform.py`, `main.py`, workflows.
The inc-shadow walker's TS17–19 default is a deliberate pilot scope (Phase 2 widens to
≥13) — out of scope here.

Hardcoded stragglers (the whole gap; nothing else found in a monorepo-wide scan):

| Consumer | Hardcoded today | Workflow |
|---|---|---|
| `scripts-reference/export_asset_tasks_excel.py` | `PROJECTS` names TS13–TS19 | pipeline-asset-tasks-export.yml |
| `scripts-reference/export_timer_excel.py` | `PROJECTS` names TS13–TS19 | pipeline-timer.yml |
| `scripts-reference/export_qa_form_excel.py` | (name, project_did) pairs | pipeline-forms.yml |
| `swift_api_pipeline/config.py` `QA_FORMS` | per-TS QA form IDs + raw table names | pipeline-forms.yml |

## Design

### 1. Exports go dynamic

Each of the three export scripts replaces its literal list with one startup query:

```sql
SELECT project_name, project_did
FROM reference.ref_ontel_techops_projects
WHERE project_number >= 13
ORDER BY project_number
```

Names/dids flow through the existing per-project loops unchanged. A ref-view query
failure aborts the export exactly like today's guard (no silent fallback to a stale list).

Exception: `export_qa_form_excel.py` sources its list from `reference.ref_qa_forms`
(section 3) joined to the ref view — a TS whose QA form isn't registered yet is simply
not in that export until discovery registers it.

### 2. Empty-project tolerance (single alarm owner)

A TS with 0 staging rows is **skipped** with a printed `SKIPPED (new/empty)` note instead
of failing the export guard. Rationale: "data was there and shrank" is already the
extract guard's job (fixed 2026-08-11 — only a >10% drop against a positive baseline
alarms). The export stops duplicating that judgment. When TS20 gets its first tasks it
appears in the next nightly workbook with zero manual steps.

### 3. QA forms: config moves to DB — `reference.ref_qa_forms`

`QA_FORMS` (config.py dict) becomes a table (migration, RLS enabled per standing rule):

```
reference.ref_qa_forms
  ts_number      int  PK
  form_id        text NOT NULL UNIQUE   -- Swift form id
  form_title     text NOT NULL          -- e.g. 'ACTIVE - QA Form TS13'
  table_name     text NOT NULL UNIQUE   -- raw_form_qa_ts13
  active         boolean NOT NULL DEFAULT true
  registered_by  text NOT NULL          -- 'seed' | 'auto-discovery'
  registered_at  timestamptz NOT NULL DEFAULT now()
```

Seeded from today's seven entries (TS13–TS19). `extract_forms.py` / `transform.py` read
the table (active rows) instead of the dict. `config.py QA_FORMS` is deleted — one source
of truth, updatable without a commit.

### 4. QA form auto-discovery via REST (spike-proven 2026-08-11)

`GET {SWIFT_BASE_URL}/api/organizations/-K5UFaiZw8e3-7nii3eT/forms` (standard bearer
auth, `pageSize`/`after` pagination, `hasMore` flag) returns all org forms — 49 today,
one page. All seven current QA form IDs returned by the endpoint match the config values
exactly. Form titles follow the strict pattern `ACTIVE - QA Form TS{n}`.

Nightly forms run, before extraction:

1. Compare `ref_ontel_techops_projects` (≥13) against `ref_qa_forms.ts_number`.
2. For each missing TS: fetch the org forms list, match `^ACTIVE - QA Form TS{n}$`
   (exact, case-sensitive).
3. **Exactly one match** → insert the `ref_qa_forms` row, create `raw_form_qa_ts{n}`
   from a version-controlled DDL template (RLS on; same precedent as asset-tasks
   partition auto-creation — deliberate, documented deviation from every-object-via-
   migration), extract the form this same run, and email a confirmation:
   *"Registered QA Form TS{n} ({form_id}) — reply if wrong."* The email is a veto,
   not a task.
4. **Zero matches** → quiet nightly retry (the form simply doesn't exist in Swift yet).
   Escalate to an alert email only when the TS project has asset-task rows flowing but
   still no QA form after 7 days.
5. **Multiple matches** → alert email listing the candidates; a human inserts the row
   (rare; the ACTIVE-prefix pattern excludes all current lookalikes).

Discovery failure (auth error, endpoint change) degrades to the alert email — never
below the alert-only baseline. `discover_forms.py` (Playwright; login fixed 2026-08-11)
remains a manual fallback tool only.

Email plumbing: existing `send_pipeline_email` / health-watcher pattern, recipient
jamil.mendez@ontel.co.

### Out of scope (deliberate)

- Inc-shadow pilot scope (TS17–19) — Phase 2 of that project widens it.
- Email-reply-to-register loop — considered, rejected as over-engineering for a
  ~2×/year event now that discovery is fully automatic; can be bolted onto the
  ambiguous-case alert later if that alert ever fires often.
- Widening the QA export/extract to TS7–TS12 historical forms — current scope is ≥13,
  unchanged.
- DARA/doc prose mentioning "TS1–TS19" — descriptive text, not runtime lists.

## Testing

- Unit: dynamic-project query helper (mocked rows); skip-empty export logic;
  discovery matcher (exact one / zero / multiple / lookalike titles); table-name
  derivation `ts_number → raw_form_qa_ts{n}`.
- Contract: `ref_qa_forms` seed matches the seven live config values (one-time,
  part of the migration verification).
- Live: nightly runs exercise the ref-view path; the TS20 case is the real-world test —
  expect auto-registration the night `ACTIVE - QA Form TS20` appears in Swift.

## Rollout

1. Migration: `ref_qa_forms` + seed + RLS (senior-dev preflight per standing rule;
   list migrations dir for next free number).
2. Exports go dynamic (3 scripts) + skip-empty guard change.
3. `extract_forms.py`/`transform.py` read `ref_qa_forms`; delete `QA_FORMS` dict.
4. Discovery + registration + emails in the forms pipeline.
5. Premerge-review before merge (standing habit); README/CHANGELOG in the same change.
