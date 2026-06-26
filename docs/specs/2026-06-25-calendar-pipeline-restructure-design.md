# Calendar Pipeline Restructure — Design Spec

**Date:** 2026-06-25
**Status:** Draft for review
**Author:** Jamil + Claude (with senior data-architect / data-engineer / data-analyst review)
**Scope decision:** Restructure (not rebuild). Phased: Phase 1 ships data-quality + rename; Phase 2 adds identity resolution + serving layer.

---

## 1. Background

The "calendar leave" pipeline extracts events from a single shared Google Calendar into the
warehouse. Despite the name, the calendar holds **more than leave**: personal availability
events (RD, VL, SL, WW, UT, SDL, half-days, compounds), public holidays, birthday markers,
training events, on-site markers, a performance eval, and blank events.

Current objects:

| Layer | Object | Notes |
|---|---|---|
| Raw | `data_raw.raw_calendar_leave` | JSONB, append-only, 58,833 rows across 123 runs |
| Staging | `data_staging.stg_calendar_leave` | 15,129 deduped rows (positional parser + Haiku value-normalization) |
| Analytics | `analytics.v_calendar_leave` | plain view, 15,129 rows |
| Analytics | `analytics.v_calendar_leave_daily` | plain view, multi-day leave exploded to daily grain, 17,987 rows |

Trigger: Google Apps Script `triggerCalendarLeave()` fires a `repository_dispatch`; recently
moved from once-daily (12:30 AM) to twice-daily (6 AM & 6 PM ET).

Consumer today: the HR system app, reading live twice daily. Stated future: other apps.

## 2. Problem

### 2.1 The cleaning is mislabeled as an "AI" problem — it is a parser problem

Cleaning runs in two separate stages:

1. **Deterministic positional parser** (`parse_summary`) splits the summary
   `"LeaveType - Team - Person (note)"` on `" - "` and **decides which column each value lands
   in**. No AI involved.
2. **Claude Haiku** (`ai_normalize`) only normalizes the *values* already in `team` /
   `leave_type` (producing `*_normalized`). It **never moves data between columns**.

So every column-misplacement is a parser defect. The AI is actually working (e.g. it correctly
maps `Ced's Birthday!` → `leave_type_normalized = NULL`).

### 2.2 Confirmed defects (with scale)

| # | Defect | Root cause | Rows |
|---|---|---|---|
| 1 | Team code glued into `person` (`VL - CG1- Angelica` → person=`CG1- Angelica`, team=NULL) | `_normalize_separators` regex `([A-Za-z])- ` only fires when a **letter** precedes the dash; teams ending in a digit (CG1/2/3) are skipped, so `" - "` split yields 2 parts | 6 |
| 2 | Non-leave events dumped whole into `leave_type` (birthdays, training, eval) | No `" - "` and no `-` → fallback dumps entire summary into `leave_type` | ~38 |
| 3 | Underscore-separated real leave (`VL_CRTV_Nicolai`) lost | Parser only knows `" - "` / bare `-`, not `_` | 1 |
| 4 | Day-of-week in `person` (`RD - Alpha - Fri`) | Source-data convention: rest-day labeled by weekday, not person. Parser positionally correct but semantically wrong | 573 |
| 5 | Unparenthesized note glued to person (`UT - TS OPS - Mik - In by 12PM`) | Notes only split when wrapped in `()` | 7 |
| 6 | Deleted events leave ghost rows | Incremental sync returns deletions as `status="cancelled"`; transform **skips** them, never removes the staging row | n/a |
| 7 | AI normalization scoped per-run | On incremental runs the model only sees that run's distinct values; output not reproducible across runs | n/a |

Of 15,129 rows, the genuinely broken parses are small (~14: defects 1, 3, blanks). Defect 4 (573)
is "working as coded" but semantically wrong. Defect 2 (~38) is scope/noise. Defects 6–7 are
correctness/operational.

### 2.3 Schema mismatch

The staging table assumes every row is one person's leave. That is why holidays, birthdays, and
rest-day-by-weekday rows read as "bugs": the schema has nowhere to put them.

## 3. Goals & non-goals

**Goals**
- Rename the dataset off "leave" so it reflects a general calendar feed.
- Make column placement robust to a free-text source (fix defects 1–5).
- Give non-leave events a proper home instead of polluting leave semantics.
- Fix deletions (no ghost rows).
- Make extraction deterministic and reproducible across incremental/full-refresh.
- Preserve the HR app's read contract (zero-downtime cutover).
- (Phase 2) Make the data joinable to HR/timer via existing identity, and add a serving layer
  other apps can build on.

**Non-goals**
- Rebuilding extract / incremental sync / raw landing (they work; keep them).
- Building new person/team dimensions — `reference.ref_employees` already exists (see §5.3).
- Speculative per-event-kind tables (birthday/training/etc.). YAGNI.
- A true leave **balance** (no entitlement/accrual source exists; we expose *consumed*, named
  honestly).

## 4. Design principles (from the senior review panel)

1. **One conformed table + `event_kind` discriminator. No per-kind physical tables.** Per-kind
   access comes from views, not tables.
2. **The confidence gate is the whole ballgame.** The fast-path must validate *semantics*
   (known leave-code, known team, no weekday-in-person, no trailing note), not just
   "did it split into 3 parts." Otherwise defects 1/4/5 survive.
3. **AI does structured extraction of the whole row shape** on the messy tail, not just value
   normalization — because the defects are *placement* bugs.
4. **Extraction is a pure function of (raw, persisted cache).** Caching keyed on the raw summary
   string makes incremental and full-refresh produce identical output and re-runs free.
5. **Reuse existing reference data; never mint dimension members at parse time.**
6. **Holidays are reference data**, curated, matched against — not reverse-engineered from
   free-text keywords.
7. **Soft-delete + reconcile against live Calendar** — a raw replay does not clean ghosts.
8. **Build beside → diff → repoint view.** Never `TRUNCATE` the live table.

---

## 5. Phase 1 — data quality + rename

### 5.1 Rename

| Old | New |
|---|---|
| `data_raw.raw_calendar_leave` | `data_raw.raw_calendar_events` |
| `data_staging.stg_calendar_leave` | `data_staging.stg_calendar_events` |
| `analytics.v_calendar_leave` | **keep the name** (preserve HR app contract); redefine over the new base, filtered to `event_kind = 'leave'` |
| `analytics.v_calendar_leave_daily` | keep for now; redefine over new base |

"Leave" survives only as a *view name / event_kind value*, never as the table identity.

### 5.2 Conformed staging table `stg_calendar_events`

Grain: **one row per source calendar event.** PK: `event_id` (Google event id; confirm whether
recurring-instance ids `{id}_{ts}` are stored and keep them distinct).

Columns (illustrative):

- Identity/lineage: `event_id` (PK), `ical_uid`, `etag`, `run_id`, `loaded_at`, `parsed_at`
- `summary_raw` — original string, **always retained** (audit + reparse seed)
- `event_kind` — CHECK-constrained domain: `leave | holiday | birthday | training | other`
  (coarse on purpose; nuance lives in the code dimension in Phase 2). Default `other`, never NULL.
- Parsed shape: `leave_type` (raw), `leave_type_normalized`, `team` (raw), `team_normalized`,
  `person`, `person_note`, `rest_day_of_week` (populated only when the event is a rest day and a
  weekday was found in the person slot — fixes defect 4)
- Dates: `start_date`, `end_date`, `days`, `is_all_day` (UTC stored; ET on display per standing rule)
- Source meta: `creator_email`, `event_created`, `event_updated`
- Quality: `parse_source` (`deterministic | ai`), `parse_confidence`, `needs_review` (bool),
  `is_deleted` (bool), `deleted_at`

CHECK constraints enforce kind semantics, e.g. `leave_type_normalized IS NULL OR event_kind = 'leave'`,
`rest_day_of_week IS NULL OR event_kind = 'leave'`, so a parser bug cannot smear non-leave rows
into leave totals.

### 5.3 Parser rebuild: deterministic fast-path + AI structured-extraction on the tail

For each event summary:

1. **Compute a cache key** = whitespace-trimmed/collapsed `summary_raw` (do not over-normalize).
2. **Cache lookup** in a persisted parse-cache table (see §5.4). Hit → use it (free, deterministic).
3. **Miss → deterministic parser** with a **strict, self-doubting confidence gate.** A row is
   "clean" only if, after correct separator normalization, it is exactly 3 parts AND
   `leave_type` is a known code AND `team` is a known team AND `person` is not a weekday AND no
   trailing note remains. Anything failing (2-part, 4+ part, unknown token, digit-trailing team,
   weekday-in-person, no separator, underscore separator) is **not** trusted.
4. **Low confidence → Haiku structured extraction** returning the whole shape at once:
   `{event_kind, leave_type, team, person, rest_day_of_week, note, confidence}`, via
   JSON-schema-constrained output (no bare-JSON crashes; parse failure → `needs_review`, never crash).
5. **Write the result to the cache** with `parse_source`, `parse_confidence`, `needs_review`,
   `model`, `prompt_version`.

This fixes defects 1–5 structurally (placement decided by either a *validated* deterministic
parse or the AI), not by adding one regex per symptom.

### 5.4 Persisted parse cache

A real table keyed on the summary string (proposed `agent.calendar_summary_parse`, marking
AI provenance; `reference` is an acceptable alternative). Columns: `summary_key` (PK), the full
extracted shape, `parse_source`, `confidence`, `needs_review`, `model`, `prompt_version`,
`extracted_at`.

Effects: incremental & full-refresh read the same cache → identical output (kills defect 7);
backfill = a few thousand distinct strings = cents of Haiku; steady-state ~99% cache hits with a
handful of new strings per run. **Cost is not a constraint at this volume — optimize for
determinism and reviewability.**

### 5.5 Deletions

Add `is_deleted` / `deleted_at`. On `status="cancelled"`, process as a **tombstone** (soft-delete
via upsert), don't skip. Serving views filter `WHERE NOT is_deleted`. Plus a periodic
**reconciliation** (e.g. the 6 AM run) comparing staging against a live full Calendar listing to
soft-delete anything no longer present — because `updatedMin` deletion detection is unreliable and
a raw replay still contains the cancelled events.

### 5.6 Watermark (carried-forward latent bug to fix)

- Source the incremental watermark from **raw**, not staging (staging is now a rebuildable artifact).
- Use full RFC3339 precision and prefer a small **overlap** (`MAX(updated) - delta`) over a
  `+1s` gap; upserts are idempotent so re-processing is harmless, skipping is data loss.
- Ensure cancelled/deleted events advance the watermark too.

### 5.7 Backfill, validation & cutover

1. Build `stg_calendar_events` **beside** the live `stg_calendar_leave` (no consumer impact).
2. Populate the parse cache from the backfill; spend QA effort here.
3. **Diff old vs new** as the gate: counts by `event_kind`; every row where person/team/leave_type
   changed. Use the known defects as a test oracle — confirm the 6 digit-team, 1 underscore,
   ~38 no-separator, 573 RD/weekday, 7 glued-note rows now land correctly. Eyeball 100% of the
   AI-extracted tail and 100% of diffs (a few hundred rows). Write a count fingerprint.
4. QA gates: (a) no row the old parser got *right* regresses; (b) all defect classes resolved;
   (c) zero unresolved `needs_review`; (d) deletion path tested with a known cancelled event;
   (e) view contract validated against what the HR app actually selects.
5. Cut over by **repointing `analytics.v_calendar_leave`** to the new base inside a transaction
   (atomic, reversible). Keep `stg_calendar_leave` frozen ~1 week as rollback insurance, then drop.
   Flip between the 6 AM/6 PM reads.

### 5.8 Operational signals

Per-run counts of `parse_source='ai'` and `needs_review`; alert when the AI rate spikes (a human
invented a new format). Cheap, and it keeps the long tail from silently rotting again.

---

## 6. Phase 2 — identity resolution + serving layer

### 6.1 Reuse existing identity (no new dimensions)

`reference.ref_employees` (108 active employees) already provides the person dimension:
`emp_id, full_name, first/last/middle, nickname, email, position, carrier_group, cluster,
division, sub_division, work_schedule, shift_schedule, employment_status, hire/resignation dates`.

There is **no standalone team table** — team lives as columns on the employee record. The
calendar "team" maps to *two* of them:
- `CG1/CG2/CG3` → `carrier_group` (`CG1 - Verizon`, `CG2 - AT&T/DISH`, `CG3 - TMO/USCC`)
- `Alpha/Beta/Gamma/Delta/Epsilon/Zeta` → `cluster`
- fuzzy tail: `T&A`→Tools&Auto, `Swift`→Swifttt, `PHI DS`→PHDSM, etc.

**Plan:** add `emp_id` (FK → `ref_employees`) to `stg_calendar_events`, resolved by a crosswalk
matching calendar `person` on nickname / first_name / email. Once a person resolves, their
authoritative carrier_group/cluster/division come from `ref_employees`. The calendar's own `team`
string is retained as a **point-in-time** attribute (people move teams). Matching is partial
(194 calendar names across 2024–2056 incl. resigned vs 108 active) → unmatched = `emp_id NULL`
+ flag, **never guessed**. Do NOT build `ref_person` / `ref_team`.

### 6.2 Leave-code dimension

A small `reference.ref_leave_code` carrying per-code classification flags: `is_rest_day`,
`counts_against_balance`, `is_paid`, `reduces_capacity`, plus a decided decomposition rule for
compounds (`UT/SL`). The Haiku normalizer maps **into** this controlled vocabulary, never mints.

### 6.3 Public holidays → `reference.ref_holidays`

Curated, **one row per holiday-date** (collapse the 274 calendar rows to unique dates), columns
`holiday_date` (PK), `name`, `region/country`, `is_observed`. Seeded/maintained deliberately;
the calendar is one input matched against it, not the authority. Holidays are a **date attribute**,
not 194 per-person rows. Pair with a blessed business-day join pattern.

### 6.4 Serving layer

- Keep `analytics.v_calendar_leave` (event grain, `event_kind='leave'`, `NOT is_deleted`).
- Rename/replace `_daily` with **`v_calendar_person_day`** — the unified person-day availability
  spine across all availability kinds (leave + rest-days), with `event_kind` + code + capacity
  flags; apps filter themselves. This is what a "team availability" feature needs (the union),
  not three reconciled sources.
- Thin per-kind convenience views (`v_calendar_holiday`, `v_calendar_birthday`) as filtered
  selects **over the same base**, never independent definitions.
- New aggregates so apps don't each re-derive them inconsistently: `v_headcount_out_per_day`,
  `v_team_coverage_day` (joins rest-days + holidays + leave for true availability),
  `v_leave_consumed_running` (per-person cumulative of balance-relevant codes — named *consumed*,
  not *balance*).

### 6.5 Rest-day rows (resolved disagreement)

Analyst proposed a `ref_team_rest_days` rules table. **Decided against:** RD is 9,057 rows and
8,484 name a real person — RD is genuinely a per-person dated event; the 573 weekday-in-person
rows are a data-entry variant. Keep them as rest-day *events* in the conformed table with
`rest_day_of_week` set and the bad person value nulled. No separate recurring-rules table (no
recurring-rule source exists). Revisit only if a real schedule-rule source appears.

---

## 7. Retention & windowing

Year distribution (staging, by `start_date`):

| Years | Rows | Personal leave | Nature |
|---|---|---|---|
| 2024 | 3,424 | 1,599 | real historical leave + attendance |
| 2025 | 2,883 | 1,682 | real history (most personal leave of any year) |
| 2026 | 1,891 | 955 | current year, in progress |
| 2027–2040 | ~530/yr | 0 | auto-projected recurring rest days only |
| 2041–2056 | 9/yr | 0 | a few annual markers projected far out |

**Decision: keep full history; do not delete prior years.** 2024–2025 is the only real
leave/attendance record (~3,300 personal-leave rows) and powers year-over-year trends, per-person
consumption, tenure, and audit. The table is ~15k rows, so there is no storage/performance reason
to trim, and raw is append-only so staging is rebuildable regardless.

The genuine noise is the **forward** end: ~6,500 rows of recurring rest days projected to 2056
with zero real leave. Bound it at the layers that lose no history:

- **Extract horizon cap (forward):** set `timeMax = today + 12 months` on the fetch so recurring
  rest days (and other recurrences) are only materialized ~12 months out. This removes the
  2027–2056 bulk at the source. 12 months is generous for genuine future-filed leave; events
  filed further out are negligible and will materialize as the window rolls forward.
- **Serving/app window:** serving views (and the HR app) default to a recent display window
  (e.g. current year or last N months) for UI, without deleting underlying rows.
- **Backfill note:** the forward cap applies to *future* extraction. Existing far-future rows
  already in raw/staging can be pruned in a one-time cleanup (or simply left to age out and be
  excluded by the serving window) — they are harmless at this volume.

## 8. Open questions for review

1. **Parse-cache home:** `agent.calendar_summary_parse` (AI provenance) vs `reference.*`. Lean `agent`.
2. **Recurring-instance event ids:** confirm whether Google recurring expansions are stored and
   keep instance ids distinct in the PK.
3. **Phase 2 trigger:** build Phase 2 immediately after Phase 1 ships, or wait for a real second
   consumer before the serving-view expansion?
4. **`event_kind` for rest day:** keep RD under `event_kind='leave'` with `is_rest_day` flag in
   the code dimension (current plan), or promote `rest_day` to its own `event_kind`?

## 9. Risks

- **Confidence gate too loose** → defects 1/4/5 survive. Mitigation: validate semantics, route
  conservatively (12–15% to AI is fine).
- **Ghost rows on backfill** if cutover relies on raw replay. Mitigation: live-Calendar reconciliation.
- **Live-table downtime** if anyone truncates. Mitigation: build-beside + view repoint.
- **Identity match rate** < 100%. Accepted: NULL + flag, never guess; surface unmatched for cleanup.
