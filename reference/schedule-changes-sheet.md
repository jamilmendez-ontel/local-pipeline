# HR Schedule-Changes Google Sheet

Committed understanding of the sheet behind `sync_schedule_changes.py`
(the sheet itself stays in Google Drive; this doc is the portable record).

- **Spreadsheet ID:** `1yX3D3ykzt8eZx6rMlCnuMf_Ei6hCt9BUh7AdBGi_U4Q`
- **Owner/editors:** HR + team leads (each team maintains its own tab)
- **Purpose:** the approved source of truth for member shift changes: one-day
  adjustments, temporary periods, and ongoing schedule moves, with approvers
  and reasons in Notes.
- **Feeds:** `data_raw.raw_schedule_changes` -> `data_staging.stg_schedule_change_history`
  -> `analytics.v_employee_schedule_history` (migration 253) -> DRMC
  `/employees/[id]` "Schedule history" section.
- **Spec:** `ai-projects/docs/superpowers/specs/2026-09-02-schedule-change-history.md`

## Shape (as of 2026-09-02)

19 tabs; 15 match the standard template and are ingested; 4 are skipped:
`Employee Data (automated -linked)` (roster lookups), `Summary (Please don't
edit)` (formula mirror of the team tabs; ingesting it would duplicate rows),
`Sheet32`, `Other Data`.

Template tabs (team names as of today): Alpha, Beta, Gamma, Epsilon, Zeta,
CG1, CG2, CG3, QPI, Research - T&A, Swifttt, DA, Accounting, Admin&Ops, and
one more per-team tab; the sync discovers tabs at runtime by header match, so
tab renames/additions need no code change.

Template columns (fixed order after the header row):
`ID Number | Names | Role | Shift Start (PHT) | Shift Start (EST) | Shift End
(PHT) | Shift End (EST) | Rest Day | Work Arrangement | Reg Hours | Shift |
Start Date | End Date | Month | Year | RDO To | Day | Notes [| STATUS PROMPT]`

## Semantics

- **Two record styles.** Non-TS tabs log ad-hoc changes (Start = End -> one
  day; End blank/`-` -> ongoing). TS/carrier tabs re-list the whole team per
  period block (e.g. Aug-31 -> Sep-25), so they are full period rosters.
- **Dates** are text (`Mar-9`, occasionally `Jan 16` or `3/9`) + a `Year`
  column that belongs to the START date. End < start means the range crossed
  Dec 31 (end year = start year + 1).
- **Notes are verbatim** (approvers, reasons, weekday exceptions like
  "TUES: 8AM-5PM"); never parsed.
- **ID Number = `reference.ref_employees.emp_id`** keyspace. A few old rows
  are name-only (resolved by unique first+last token match; `Noemi Longasa`
  and `Omar Junio Jr.` are known-unresolvable and stay raw-only). Some ids are
  absent from the roster (resigned before the roster sync era) and are kept.
- The sheet repeats some period blocks verbatim (17 literal duplicate rows as
  of today); the sync keeps the first occurrence.

## Fingerprints (2026-09-02 first load)

- 1,335 raw rows, 1,310 staged rows, 125 distinct members, 75 is_current rows.
- Skips: 8 unresolved names, 17 in-sheet duplicates, 1 blank start date,
  3 rows with a blank PHT-start cell (kept; PK column coalesced to '').

## Running the sync

```
cd swift_api_pipeline
SCHEDULE_SHEETS_TOKEN=gmail_credentials/sheets_rw_token.pickle \
SUPABASE_HOST=aws-0-ap-southeast-1.pooler.supabase.com \
SUPABASE_PORT=5432 SUPABASE_USER=postgres.voqfjfngdpcvevbkikud \
python sync_schedule_changes.py [--dry-run]
```

The token must be Sheets-scoped: `sheets_rw_token.pickle` (the roster-gap
watcher's RW token) works; `sheets_token.pickle` does NOT (drive.readonly
only, and the sync needs the Sheets v4 API for multi-tab reads). Direct DB
host needs Cloudflare WARP; the pooler override above works without it.
v1 is run-on-demand: no workflow/trigger wired yet (deliberate deferral).
