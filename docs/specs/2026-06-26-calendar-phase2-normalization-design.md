# Calendar Phase 2: Normalization and Enrichment

Date: 2026-06-26
Status: Approved (design), pending implementation plan
Repo: `local-pipeline` (public), pipeline code in `swift_api_pipeline/`
Predecessor: Phase 1 restructure (`docs/specs/2026-06-25-calendar-pipeline-restructure-design.md`), shipped + cut over 2026-06-26 (migration 125).

## 1. Context and problem

Phase 1 delivered a working, clean calendar pipeline: deterministic parse plus AI tail,
the conformed `data_staging.stg_calendar_events` table, soft-delete/reconcile, and the
`analytics.v_calendar_leave` / `v_calendar_leave_daily` views cut over to it. Leave data
served today is correct: `leave_type`, `team`, `person`, dates, and leave/cancelled logic
are all populated and accurate.

Phase 1 deliberately left two columns empty, marked in `calendar_events_transform.py` as
"populated in Phase 2 (ref_leave_code)":

- `leave_type_normalized`: NULL for all 10,442 active leave rows.
- `team_normalized`: NULL for ~96% of rows (10,457 of 10,857).

Phase 2 builds that normalization layer. Scope confirmed with the user covers five
data-quality dimensions on the same table:

1. `leave_type_normalized` (empty): expand codes to full names.
2. `team_normalized` (empty): collapse casing/synonym variants to a canonical org label.
3. `person` dedupe: 172 distinct `person` strings vs ~108 real employees, reconcile to
   canonical names in `reference.ref_employees`.
4. `person_note` cleanup: 110 distinct values collapse to ~30 semantic ones (the same
   time-window note appears in 4 casings, e.g. `2pm onwards` / `2PM onwards` /
   `2PM Onwards` / `2 PM Onwards`).
5. RD rest-day rows: `person` is NULL by design (team rest-day markers like
   `RD - Alpha - Mon`), but `team_normalized` should still be filled.

Parse quality is already good and is NOT a Phase 2 concern: 10,639 deterministic rows,
218 AI rows, 0 rows flagged `needs_review`.

## 2. Goals and non-goals

Goals:
- Fill `leave_type_normalized` and `team_normalized` for every row where the source value
  is known and mappable.
- Add a canonical person identity (`person_normalized`, `emp_id`) reconciled to
  `ref_employees`, including resigned employees.
- Add a cleaned `person_note_normalized`.
- Make all of the above durable: every future pipeline run enriches automatically, and a
  one-time backfill enriches the existing 10,857 rows.
- Keep raw columns (`leave_type`, `team`, `person`, `person_note`) untouched as the
  source of truth.

Non-goals:
- No re-parsing of summaries (Phase 1 parse is sound).
- No new person/team dimension tables; reuse `reference.ref_employees` and its taxonomy.
- No structured partial-day time fields for `person_note` (cleaned string only; revisit
  only if partial-day windows need to be queried).
- No GHA workflow change (same `extract_calendar_events.py`).

## 3. Architecture

Chosen approach: reference-table-driven normalization, materialized in the transform.
(Rejected alternatives: view-layer joins, which cannot do fuzzy person matching and add
per-read join cost; and a one-time cleanup pass, which is not durable.)

Normalization runs as a step inside `calendar_events_transform.py`. At run start the
transform loads four lookups into memory; for each row it calls pure functions in a new
`calendar_normalize.py` module to compute the normalized columns. The same code path
serves both new runs and the one-time backfill. Within a row, person matching runs before
team derivation, because `team_normalized` is derived from the matched employee
(see 5.1a).

Lookups loaded per run:
- `reference.ref_leave_code`
- `reference.ref_calendar_team`
- `reference.ref_employees` (built into a match index)
- `agent.calendar_person_match` (AI match cache)

## 4. Data model

### 4.1 New reference table: `reference.ref_leave_code`

Drives `leave_type_normalized`. Case-insensitive lookup. PK on `code`.

Columns: `code` (PK, upper-case canonical), `code_num` (the official numeric prefix, e.g.
`003`, nullable), `label` (nullable, full name), `category` (leave / rest / work /
overtime / holiday / compound), `scope_note` (e.g. "TS Team only"), `requires_rtw_form`
(boolean, Return-to-Work form required before resuming), `is_active`, `created_at`,
`updated_at`.

Seed from the authoritative HR daily-report code legend (provided by the user 2026-06-26):

| code | code_num | label | category | scope / note |
|---|---|---|---|---|
| RDOT | 001 | Rest Day Overtime | overtime | |
| RDO | 002 | Rest Day Offset | rest | |
| VL | 003 | Vacation Leave | leave | |
| SL | 004 | Sick Leave | leave | RTW form required |
| EL | 005 | Emergency Leave | leave | RTW form required |
| SDL | 006 | Sudden Leave | leave | |
| UT | 007 | Undertime | leave | |
| BL | 008 | Birthday Leave | leave | |
| ML | 009 | Maternity Leave | leave | start date only; RTW form required |
| PL | 010 | Paternity Leave | leave | |
| SPL | 011 | Solo Parent Leave | leave | |
| BRL | 013 | Bereavement Leave | leave | |
| LR | 015 | Weekend Live Review | work | TS Team only |
| WW | 016 | Weekend Work | work | TS Team only |
| LRWD | 017 | Weekday Live Review | work | TS Team only |
| LDL | 018 | Learning & Development Leave | leave | |
| LDO | 019 | Learning & Development Overtime | overtime | |
| RD | (none) | Rest Day | rest | scheduled rest-day marker, not a requestable code |

`LRWD`, `LDL`, `LDO` do not yet appear in the data but are seeded for completeness.
`RD` (the most common code, 4,857 rows) is the scheduled rest-day marker; it is not in the
requestable legend but is a valid, well-understood value, so it is seeded explicitly.

Codes present in the data but NOT in the official legend are treated as legacy/unknown and
seeded with `label = NULL` (flagged for HR), so `leave_type_normalized` falls back to the
raw code for them: `LAC` (94 rows), `STL` (2), `HD` (2), `LWOP` (1). These are listed in
Section 10 as open items, in case any need a definition or are deprecated.

Compound codes (`UT/SL`, `LAC/UT`, `VL / LAC`, `UT/EL`, `UT/SDL`, `EL/SL`, `UT/HD`) are
not stored as rows. `normalize_leave_type` splits on `/`, trims, looks up each part, joins
the labels with ` + ` (for example "Undertime + Sick Leave"), and tags `category=compound`.
When any part is unknown, that part falls back to its raw code in the joined label.

Fallback rule: when a code has no row or a NULL label, `leave_type_normalized` falls back
to the raw code. The column is therefore never empty for a known leave row; unknown codes
are simply un-expanded until HR fills the label.

### 4.2 New reference table: `reference.ref_calendar_team`

This is the FALLBACK source for `team_normalized`. The primary source is the matched
employee's `carrier_group` (see 5.1a); `ref_calendar_team` is consulted only when there is
no matched person (RD rest-day rows, or unmatched people). It also normalizes the label
casing/synonyms for those fallback rows.

Rationale: the empirical check (2026-06-26) showed the calendar's team label is sometimes a
status rather than a team (`Trainee` people span CG1/CG2/CG3 and several clusters) or is
simply wrong (a person tagged `Marketing` who is really TSPM/Delta). The person's actual
`ref_employees.carrier_group` is the authoritative team, so the label is fallback-only.

Case-insensitive, trimmed lookup. PK on `team_raw`. `team_normalized` uses the full
canonical label (for example `CG1 - Verizon`) so it ties to the `ref_employees` taxonomy.

Columns: `team_raw` (PK, the variant as it appears), `team_canonical` (full canonical
label from the `ref_employees` taxonomy), `level` (carrier_group / cluster / department),
`created_at`, `updated_at`.

Seed mapping (variants on the left collapse to the canonical on the right):

| raw variants | team_canonical | level |
|---|---|---|
| CG1 | CG1 - Verizon | carrier_group |
| CG2 | CG2 - AT&T/DISH | carrier_group |
| CG3 | CG3 - TMO/USCC | carrier_group |
| Acctg, ACCTG, Accounting | Accounting | carrier_group |
| Admin and Ops, Admin & Ops | Admin and Operations | carrier_group |
| T&A, TNA | Tools&Auto | carrier_group |
| CRTV, CRTVS | Creatives | carrier_group |
| R&D | Research | carrier_group |
| QPI | QPI | carrier_group |
| DA | DA | carrier_group |
| HR | HR | carrier_group |
| TS Admin | TS-Admin | carrier_group |
| DSM, PHIDSM, PHIDS, PHI DS | PHDSM | carrier_group |
| Swift | Swifttt | carrier_group |
| Alpha, ALPHA | Alpha | cluster |
| Beta, BETA | Beta | cluster |
| Gamma, GAMMA | Gamma | cluster |
| Delta | Delta | cluster |
| Epsilon | Epsilon | cluster |
| Zeta, ZETA | Zeta | cluster |
| MKTG, Marketing, MARKETING | Marketing | department |
| PHI HR | HR | carrier_group |
| T&D | Swifttt | carrier_group |
| Trainee | (NULL) | status, not a team |
| SD | (NULL, confirm) | unknown |
| TS Ops, TS OPS | (NULL, confirm) | unknown |

Canonicals for these were derived from the empirical check of who appears under each label:
`PHI HR` person (Orville) is carrier_group `HR`; `T&D` people (Francis, Jehane) are
`Swifttt`/Development Operations; `Marketing` is its own department not in the taxonomy.
`Trainee` is a status (its people belong to many real teams), so as a fallback label it
maps to NULL and is resolved per-person instead. `SD` (Emman) and `TS Ops` (Mik) people are
not in `ref_employees`, so they stay NULL and flagged. Unmapped `team_raw` values leave
`team_normalized` NULL (logged); we never invent a mapping.

### 4.3 New columns on `data_staging.stg_calendar_events`

Added via migration. Raw columns are unchanged.

- `person_normalized` text: canonical `full_name` from `ref_employees`, else NULL.
- `emp_id` text: matched employee id, else NULL.
- `person_match_source` text: `exact` / `ai` / `unmatched`.
- `team_level` text: `carrier_group` / `cluster` / `department`, indicating what
  `team_normalized` represents (carrier_group when person-derived).
- `person_note_normalized` text: cleaned note, else NULL.

(`leave_type_normalized` and `team_normalized` already exist and get filled.)

### 4.4 New cache table: `agent.calendar_person_match`

Mirrors the existing parse-cache pattern so AI person matches are deterministic and cheap.
PK on `(person_raw, team_raw)`.

Columns: `person_raw`, `team_raw`, `emp_id`, `person_normalized`, `confidence`,
`resolved_at`, plus standard audit columns. Lookups hit the cache first; only genuine
misses call Haiku, and the result is written back.

## 5. Normalization logic (`calendar_normalize.py`)

Pure functions, no DB or network inside them (lookups are passed in), so they are unit
testable in isolation.

- `normalize_leave_type(code, code_map) -> (label, category)`: case-insensitive; compound
  split on `/`; fallback to raw code.
- `normalize_team(emp_match, team_raw, team_map) -> (canonical, level)`: PERSON-DERIVED.
  When `emp_match` is present (person matched), return the employee's `carrier_group`.
  Otherwise fall back to a case-insensitive, trimmed lookup of `team_raw` in `team_map`
  (RD rest-day rows, unmatched people). NULL when neither yields a value. See 5.1a.
- `normalize_person(person_raw, team_raw, emp_index, ai_cache) -> (person_normalized,
  emp_id, source)`: see 5.1. Person matching runs BEFORE team so its result feeds team.
- `normalize_person_note(note_raw) -> normalized`: see 5.2.

### 5.1 Person matching

Build an in-memory index from `ref_employees` keyed by lower-cased `nickname`,
`first_name`, and `full_name`, including resigned employees (historical leave references
people who have since left). Calendar `person` values are usually a first name or nickname
(`Ed`, `Prince`, `Lourvina`).

Match priority:
1. Exact nickname, then first_name, then full_name (case-insensitive).
2. Disambiguate by team: when more than one employee shares a name, use the row's
   normalized team or cluster to select the correct employee. This resolves most ambiguity
   deterministically.
3. AI fallback (Haiku) for the genuine tail (typos, unusual nicknames). Result cached in
   `agent.calendar_person_match` keyed on `(person_raw, team_raw)`.
4. No confident match: `person_normalized` NULL, `emp_id` NULL,
   `person_match_source = unmatched`; raw `person` preserved.

RD rows and other rows where `person` is NULL by design are skipped (source stays NULL).

### 5.1a Team derivation (depends on 5.1)

`team_normalized` is derived from the matched person, not the calendar label:
- Person matched: `team_normalized` = that employee's `carrier_group` (e.g. `CG1 - Verizon`,
  `Accounting`), `team_level = carrier_group`. This auto-corrects mislabels (a person
  tagged `Marketing` who is really TSPM resolves to their true carrier_group) and resolves
  status labels like `Trainee` to each trainee's real team.
- No matched person (RD rest-day rows like `RD - Alpha - Mon`, or unmatched people): fall
  back to `ref_calendar_team` on the raw label. RD rows carry a cluster (Alpha..Zeta) in the
  team position, which maps cleanly.
- Neither yields a value: `team_normalized` NULL (logged).

Cluster granularity (Alpha/Beta) is not stored separately in this phase; it remains
available via a join to `ref_employees` if needed later (YAGNI for now).

### 5.2 person_note normalization

Deterministic regex. Uppercase `AM`/`PM`, normalize spacing and casing so the time-window
variants collapse to one canonical string (for example `2pm onwards`, `2PM onwards`,
`2PM Onwards`, `2 PM Onwards` all become `2:00 PM onwards`). Raw `person_note` is
preserved; the cleaned value goes to `person_note_normalized`. Notes that do not match a
known pattern (for example `Weekend Work`, `Working AM`) are passed through with casing
standardized only.

## 6. Migrations

- `126_ref_leave_code`: create + seed `reference.ref_leave_code`.
- `127_ref_calendar_team`: create + seed `reference.ref_calendar_team`.
- `128_calendar_events_normalize_columns`: add the four columns to
  `data_staging.stg_calendar_events`; create `agent.calendar_person_match`.
- `129_v_calendar_leave_normalized`: add the new columns to `analytics.v_calendar_leave`
  and `analytics.v_calendar_leave_daily`.

All migrations applied via Supabase MCP (WARP-vs-Claude constraint). They follow
`DATABASE_ARCHITECTURE.md`: `ref_*` naming, PK on every upserted table, and they touch
only our own schemas (`reference`, `data_staging`, `analytics`, `agent`). New tables match
the RLS posture of their existing schema.

## 7. Backfill

Add a `--renormalize` mode to `extract_calendar_events.py` that re-enriches existing
staging rows in place: read the raw `leave_type` / `team` / `person` / `person_note`
already present, compute normalized values, and bulk `UPDATE` by `event_id`. No Calendar
re-fetch. Idempotent and re-runnable. Executed via GHA (or MCP), consistent with the
Phase 1 backfill. Datetimes shown to the user in any verification output are converted to
America/New_York.

## 8. Implementation waves

Because `team_normalized` is now person-derived, person matching is foundational and moves
into Wave 1 (it cannot be a later wave). Revised plan:

1. Core normalization: migrations 126/127/128/129. In the transform, fill
   `leave_type_normalized` (deterministic), run person matching (deterministic + AI cache)
   to fill `person_normalized` / `emp_id` / `person_match_source`, then derive
   `team_normalized` from the matched employee with label fallback. Backfill the 10,857
   existing rows. This wave fills both empty columns the user is looking at, plus the new
   person columns.
2. Notes: `person_note_normalized` regex, backfill. Independent and shippable separately.

`leave_type_normalized` is independent of person matching, so it can be implemented and
backfilled first within Wave 1 as an early checkpoint if desired.

## 9. Testing

TDD. Unit tests for `calendar_normalize.py`:
- leave codes: known, unknown (raw fallback), casing (`WW` vs `ww`), compound (`UT/SL`).
- team: every variant in the seed maps to the right canonical; unmapped stays NULL.
- person: exact nickname/first_name/full_name; team-disambiguation of shared names;
  unmatched path; resigned-employee match.
- person_note: the four casings of a window collapse to one; pass-through notes.

Post-backfill data-quality checks (via MCP):
- `team_normalized` non-NULL wherever `team` is mapped; list any unmapped `team_raw`.
- `leave_type_normalized` non-NULL for every known code; list codes with NULL label.
- row counts unchanged; no regression vs current `v_calendar_leave`.
- `person_match_source` distribution (exact / ai / unmatched) reported.

## 10. Open items requiring user input

- Leave codes RESOLVED via the authoritative HR legend (2026-06-26). Remaining: a few
  codes appear in the data but not the official legend, seeded NULL and flagged:
  `LAC` (94 rows), `STL` (2), `HD` (2), `LWOP` (1). Confirm whether these are legacy/
  deprecated or need definitions. Not a blocker (they fall back to the raw code).
- Team labels mostly RESOLVED by person-derivation + the empirical check. Remaining
  fallback-label unknowns (only matter for unmatched people): `SD` (Emman) and `TS Ops`
  (Mik) are not in `ref_employees`. Confirm their canonical team, or accept NULL. Also
  worth confirming whether `Marketing`, `SD`, `TS Ops` people should be added to
  `ref_employees` so they match like everyone else.
