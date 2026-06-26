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
serves both new runs and the one-time backfill.

Lookups loaded per run:
- `reference.ref_leave_code`
- `reference.ref_calendar_team`
- `reference.ref_employees` (built into a match index)
- `agent.calendar_person_match` (AI match cache)

## 4. Data model

### 4.1 New reference table: `reference.ref_leave_code`

Drives `leave_type_normalized`. Case-insensitive lookup. PK on `code`.

Columns: `code` (PK, upper-case canonical), `label` (nullable, full name), `category`
(e.g. leave / rest / work / holiday / compound), `is_active`, `created_at`, `updated_at`.

Seed (DRAFT; labels marked NULL need HR confirmation and are seeded NULL, not guessed):

| code | label | category |
|---|---|---|
| RD | Rest Day | rest |
| VL | Vacation Leave | leave |
| SL | Sick Leave | leave |
| EL | Emergency Leave | leave |
| UT | Undertime | leave |
| BL | Birthday Leave | leave |
| WW | Weekend Work | work |
| LWOP | Leave Without Pay | leave |
| HD | Half Day | leave |
| ML | Maternity Leave | leave |
| PL | Paternity Leave | leave |
| BRL | Bereavement Leave | leave |
| SPL | Solo Parent Leave | leave |
| PH | Public Holiday | holiday |
| RDO | (NULL, confirm) | rest |
| RDOT | (NULL, confirm) | rest |
| SDL | (NULL, confirm) | leave |
| LAC | (NULL, confirm) | leave |
| STL | (NULL, confirm) | leave |
| LR | (NULL, confirm) | leave |

Compound codes (`UT/SL`, `LAC/UT`, `VL / LAC`, `UT/EL`, `UT/SDL`, `EL/SL`, `UT/HD`) are
not stored as rows. `normalize_leave_type` splits on `/`, trims, looks up each part, joins
the labels with ` + ` (for example "Undertime + Sick Leave"), and tags `category=compound`.
When any part is unknown, that part falls back to its raw code in the joined label.

Fallback rule: when a code has no row or a NULL label, `leave_type_normalized` falls back
to the raw code. The column is therefore never empty for a known leave row; unknown codes
are simply un-expanded until HR fills the label.

### 4.2 New reference table: `reference.ref_calendar_team`

Drives `team_normalized`. Case-insensitive, trimmed lookup. PK on `team_raw`.

Columns: `team_raw` (PK, the variant as it appears), `team_canonical` (full canonical
label from the `ref_employees` taxonomy), `level` (carrier_group / cluster / department),
`created_at`, `updated_at`.

`team_normalized` uses the full canonical label (for example `CG1 - Verizon`), per the
user's decision, so it ties directly to the org taxonomy in `ref_employees.carrier_group`
and `ref_employees.cluster`.

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
| MKTG, Marketing, MARKETING | Marketing | department (confirm) |
| SD | SD (confirm) | department (confirm) |
| T&D | T&D (confirm) | department (confirm) |
| Trainee | Trainee (confirm) | department (confirm) |
| TS Ops, TS OPS | TS-Ops (confirm) | department (confirm) |
| PHI HR | PHI-HR (confirm) | department (confirm) |

Variants marked "confirm" have no exact `ref_employees.carrier_group` match; the proposed
canonical is a best guess for the user to confirm during seeding. Unmapped `team_raw`
values fall back to leaving `team_normalized` NULL (logged), so we never invent a mapping.

### 4.3 New columns on `data_staging.stg_calendar_events`

Added via migration. Raw columns are unchanged.

- `person_normalized` text: canonical `full_name` from `ref_employees`, else NULL.
- `emp_id` text: matched employee id, else NULL.
- `person_match_source` text: `exact` / `ai` / `unmatched`.
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
- `normalize_team(team_raw, team_map) -> (canonical, level)`: case-insensitive, trimmed;
  fallback to NULL when unmapped.
- `normalize_person(person_raw, team_raw, emp_index, ai_cache) -> (person_normalized,
  emp_id, source)`: see 5.1.
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

Each wave is independently shippable. Wave 1 alone fills the two empty columns the user is
looking at.

1. Categorical: migrations 126/127, leave_type + team normalization in the transform,
   backfill. View update (129) can land here to expose all new columns, filling
   progressively across waves.
2. Person: migration 128, matching module + AI cache, backfill.
3. Notes: `person_note_normalized` regex, backfill.

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

- Leave-code labels: `RDO`, `RDOT`, `SDL`, `LAC`, `STL`, `LR` (seeded NULL until provided).
- Team canonical labels for variants without an exact taxonomy match: `MKTG`/`Marketing`,
  `SD`, `T&D`, `Trainee`, `TS Ops`, `PHI HR`.
