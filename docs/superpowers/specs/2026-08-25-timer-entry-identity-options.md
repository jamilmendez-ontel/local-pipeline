# Timer entry identity: decision document

Prepared 2026-08-24 for Jamil. Inputs: three ground reports (Swift endpoint probe, identity map of `timer_correction_review.py`, 60-day forensic on `stg_timer_activities`), four design options, three judge lenses (correctness, migration/ops risk, technician experience). Read-only investigation; nothing in the repo or the DB was changed.

## 1. The problem in one paragraph

Today's `entry_id` is a 12-char md5 of the FULL natural key (`project_did|user_email|start_time|site_name|site_id|task|end_time|duration_min`), which means it is a fingerprint of one nightly snapshot, not an identity for a timer. Anything that changes any of those eight fields mints a new id, and in the last 60 days that happened constantly: 28 NULL-end timers completed 2-3 days after start (new end, new hash), 29 late sibling rows appeared on existing start-keys (25 of them longer, 16 pushing the group over 12h), 75 start-keys had their `site_id`/`site_name` rewritten by Swift with everything else unchanged, 96.1% of rows (23,266 of 24,219) carry a 48-61 char binary-float duration tail that is part of the hash, and the whole current month's surrogate ids are reassigned every night by `DELETE FROM stg_timer_activities WHERE start_date = $1` + reinsert. The consequences are on file: 34 of 1,448 removals (2.3%) and 22 of 278 corrections (7.9%) in 60 days no longer match a live row; 794 non-REVERTED removals all-time are inert; 544 of 1,582 corrections (34%) have lost their row and are materialised as 550 virtual clean rows; 26 removals and 18 corrections are keyed to a NULL `end_time` and go dead the moment the timer completes; 11 removal rows carry a stale response hash that is not even f(natural key); and, the incident that triggered this, a member's Remove on one row of a duplicate group made `_resolve_duplicate_for_action` (L1650-1652) and `_reconcile_resolved_group_stragglers` (L2085) crown the LATEST `end_time` snapshot as survivor, which in 21 of 21 runaway groups per snapshot is the runaway (avg 2,387 vs 142 min), and auto-wrote `entry_removals` rows for the real session. The 2026-08-10 22.6h over-removal was the mirror image: a start-key match deleted real sessions next to ghosts. Four different identity functions coexist in the review script (`_make_entry_id`, `_make_group_id`, `_start_key`, `_make_resend_stable_key`) plus two SQL re-implementations that do full-table md5 scans, and 53 byte-identical full keys in 60 days map to 2+ physical rows, so the current id cannot even distinguish exact twins. The md5 is a lookup convenience that was promoted to an identity; it never was one.

## 2. What Swift gives us

Verdict: **Swift does mint a stable per-entry id, but not on the endpoint we read.** Two reports said "no upstream id"; both were looking at the right place and were right about it. The third report found the other endpoint.

- `GET /api/timer-activities/_report` (what `extract_timer.py:156` calls) returns flattened rows with exactly 13 keys: Project, Site Name, Site ID, Task, Start Time, End Time, Duration (min), User Name, User Email, User Role, User Lat/Long/Accuracy (m). Across all 1,300,421 raw rows / 223 runs since 2026-02-11, zero rows have any id-like key (exact-name and regex probes both 0). Nothing is dropped client-side; the payload simply has no id.
- `GET /api/timer-activities` (no `_report`, same `filterOptions`, `pageSize` max 500, bare JSON array, 401 without a project filter) returns the underlying Firebase records WITH `id` (20-char push id, e.g. `-P-augFDHzlF7D5cFddJ`), plus `start`/`end` epoch ms, `user` (auth0 id), `target.id` (task DID), `asset`, `ETag`. Nothing in the repo calls it today.
- The same id shape is ALREADY treated as a stable key elsewhere in our code: `extract_daily_reports.py:197` reads `/api/asset-tasks/{task_did}/timer-activities` and upserts `stg_daily_report_attendance ON CONFLICT (task_did, timer_id)`; 18,080 rows, 18,080 distinct ids, only 7 NULL-end (completed versions overwrite in place under the same id).
- Entity rows map 1:1 onto report rows on (byName, floor(start_ms/1000), floor(end_ms/1000)): TS19 2026-08-21 493=493 matched 493/493; 5-day window 08-17..21 3,088=3,088 with 3,088 distinct ids.
- The id is minted at timer START and survives completion: push-id creation time minus `start` is median +1.0s, 429/493 within 5s; the 3 running rows on 2026-08-24 have creation time == start.
- Twins are NOT the same record re-emitted: every same-start pair is two Swift records with distinct ids (31/31 twin groups; 1,394/1,394 in DR attendance). Mechanism: the second record is created at STOP time (creation ~= its own end, seconds long) and carries the original start; the original record stays open and is closed much later, typically when the member's next timer starts (example: 20:08:58 start, runaway closed 20:41:43, next timer started 20:41:54, 11s later). So "latest end_time" is structurally the runaway.
- Member identity in the entity payload is auth0, not email; `reference.ref_employee_emails.auth0_id` covers all 83 distinct August timer emails (0 unmapped), 6/6 probed ids resolve.
- Firebase RTDB direct is a dead end with our token (401 on every guessed path).

Caveats that matter for the decision: the entity endpoint was probed for 2026-08-17..24 on TS19 only; historical reach and the silent-truncation threshold are unknown; and attaching the native id to a report row still requires pairing on (user, start second, end second), which is exactly the tuple a snapshot key would hash anyway. The native id is therefore excellent **evidence** (creation time settles real-vs-runaway without a heuristic) and a good **durable key** once attached, but it is not a free lunch: it adds a second endpoint, a mapping dependency, and a nightly pairing step whose failure mode is silent fallback.

## 3. Options

Scenario table columns: (1) running timer completes, (2) twins seconds apart, (3) runaway next to real, (4) monthly DELETE+REINSERT, (5) precision tail flip, (6) ghost NULL-end rows, (7) Remove one of a duplicate group.

### Option 1: Swift-native `swift_timer_id`

**Id.** The Swift entity `id`: exactly 20 chars from the Firebase push-id alphabet (`-0-9A-Za-z_`), e.g. `-P-augFDHzlF7D5cFddJ`. Transported as `sw:<id>` (23 chars) in the form; stored without prefix. Rows that cannot be paired keep `swift_timer_id NULL` and use the legacy md5 end to end (dual path everywhere).

**Remove/Edit resolution.** Form ref parsed to `('swift', id)` or `('md5', id)`. Swift refs: `corrections` by id, then `entry_removals` by id, then `stg_timer_activities WHERE swift_timer_id = $1` (indexed), then reconstruction from `raw_timer_entities`, else skip. Upserts `ON CONFLICT (swift_timer_id) WHERE swift_timer_id IS NOT NULL`, writing `entry_id = swift_timer_id`. Duplicate label match by id; survivor for 2-row groups = the other; for 3+ = exclude >720 min when a <=720 sibling exists, then longest. Stragglers never displace a selected record unless it is >720 and the straggler is <=720.

**What changes.** New `data_raw.raw_timer_entities` (PK swift_timer_id, upsert by id, ETag, decoded `swift_created_at`); `swift_timer_id` + `task_did` columns on `stg_timer_activities`, `_clean`, and the three action tables (partial UNIQUE); new `data_staging.attach_swift_timer_ids(run_id)` pairing on (project, user via auth0 or byName, start_s, end_s) with row_number tiebreak; second extractor pass per (project, day) at pageSize 500; `--entities-only` backfill CLI; ref parsing, lookups, upserts, duplicate resolver, straggler reconcile, `auto_resolve_stale` unification, resend key `<id>|<end_s>` with a one-off snapshot rebase; every rebuild anti-join becomes `(id match) OR (legacy natural-key match when id NULL)`; watcher match-rate alert at 99.5%. Forms and Apps Script unchanged. Migrations 242 (DDL) / 243 (data backfill, passes A-D, collision log) / 244 (rebuild).

**Seven scenarios.**

| # | Behaviour |
|---|---|
| 1 | Same record, same id; resend key keeps end_s so NEW still fires. Correct. |
| 2 | Distinct ids; Remove hits one record. Correct (binding to "what they saw" still comes from end-second pairing). |
| 3 | Distinct ids; clicked record rejected; 3+ groups use "exclude >720 then longest" (heuristic). |
| 4 | Re-attached nightly from `raw_timer_entities`, which is never reloaded. Correct. |
| 5 | Id has no duration. Correct. |
| 6 | Ghost is its own Swift record; removal cannot touch the completed sibling. Correct. |
| 7 | Exact by id; survivor rule for 3+ is still a heuristic. |

**Migration of existing data.** Order: 242 -> entity backfill from 2026-02 (staging before 2026-02-11 can never get ids) -> attach every month -> 243 -> 244 -> snapshot rebase -> flip email to `sw:`. 243 backfills from natural-key COLUMNS in four passes (exact key; start-key + end second; start-key alone when one entity; user+start+task+project ignoring site when one entity); collisions logged to `app_timer.id_backfill_log`. Pass C deliberately re-attaches the 26 NULL-end removals to the completed record, which reverses spec section 6 and migration 241 (flagged as a Jamil decision). Expected: 794 inert removals shrink to true Swift deletions + pre-window rows; 550 virtual rows shrink to genuinely vanished records.

**Effort.** 9-11 engineer-days plus 2-3 shadow nights. **Risk.** Medium (dual path degrades to today, not to a wrong match), but identity now depends on an undocumented second endpoint, on `ref_employee_emails.auth0_id` coverage, and on Swift never re-minting ids.

**Judge scores.** Correctness 7/10 (best physical identity; same survivor heuristics; pass C is a time-loss path). Ops risk 7/10 (best shadow story; longest cutover chain; dual-path predicates may defeat indexes; reverses 241). Technician 5/10 (email is explicitly unchanged, so the affordance that invites N Remove clicks per timer is untouched; id is 20 opaque chars). Rankings: 3rd, 2nd, 4th.

### Option 2: Warehouse-assigned lineage registry (`app_timer.entry_lineage` + `entry_snapshot`)

**Id.** `lineage_id = 'L' || left(md5(project_did|user_email|epoch(start_time)|coalesce(task,'')),14)` (15 chars), persisted in `entry_lineage`; `snapshot_no smallint` assigned by `entry_snapshot` the first time a distinct (lineage, end_sec, duration_2dp) is observed, ordered by first-seen run then end ASC NULLS LAST, then FROZEN forever. Token `L3f9a1c2b7d4e01.2`, sent as `id:L...`. Site fields excluded from identity (display only).

**Remove/Edit resolution.** Point read on (lineage_id, snapshot_no) against staging, else against `entry_snapshot` itself (holds end/duration/site after the raw row vanished, so an action can ALWAYS be recorded). Action tables upsert on UNIQUE (lineage_id, snapshot_no). Remove rejects the clicked snapshot; one sibling left -> selected; 2+ left -> group stays pending, member re-asked, provisional = SHORTEST non-zero non-runaway. Stragglers appended with the next ordinal, never displace a member selection.

**What changes.** Two new tables, `register_timer_snapshots(run_id)` called inside the transform under `pg_advisory_xact_lock` and RAISING if any row is left unstamped, seed by replaying 1.30M raw rows in run_date order; columns on staging/clean/action tables with NOT NULL after backfill; `duplicate_reviews` gains lineage_id/provisional/selected snapshot_no; every rejection path writes `entry_removals` rows (single exclusion source); rebuild Step 1 filters `lineage_id IS NOT NULL`; transition-month OR-branches; migrations 242-245 plus a resend-snapshot rebase.

**Seven scenarios.**

| # | Behaviour |
|---|---|
| 1 | NULL-end = snapshot n, completion = n+1; legacy NULL-end removals stay inert by design (spec 6). Correct. |
| 2 | Two frozen ordinals; Remove .1 rejects .1 only. Exact. |
| 3 | Distinct ordinals; member click is the decision; late runaway auto-rejected only if a member selected or it is >720 beside <=720. |
| 4 | Registry outside data_staging re-stamps nightly. Correct IF the function completes; a partial stamp silently drops rows from clean. |
| 5 | 2 dp + seconds. Correct. |
| 6 | kind='ghost', own snapshot; snapshot-scoped removal. Correct. |
| 7 | Exact; 2+ remaining -> pending again (least heuristic post-action); but provisional/auto default = SHORTEST. |

**Migration.** 242 seeds from raw replay (~400k lineages / ~410k snapshots), registers vanished keys from the action tables so the 794 inert removals and 544 orphans get real ordinals; 243 backfills action tables from natural columns, SET NOT NULL, merges collisions ('MERGED_242'), backfills `entry_removals` from `rejected_entries`; 244 rebuild; 245 drops legacy branches a month later.

**Effort.** 12-14 engineer-days, ~3 calendar weeks. **Risk.** Medium-high.

**Judge scores.** Correctness 6.5/10 (the shortest-plausible provisional rule decides hundreds of un-acted groups per month and errs toward LOSING time; 781 "other" groups per 60 days have ends 5 min to 25 h apart). Ops risk 5/10 (frozen replay-assigned ordinals are irreversible; a RAISE inside the nightly transform turns a registry bug into a pipeline outage; 4-5 migrations). Technician 6/10 (`.2` means nothing to a tech; rows still per snapshot with per-row Remove; 3+ groups reopen and add a decision). Rankings: 4th, 3rd, 3rd.

**Blunt take.** This is the most complete model on paper and the worst trade in practice. It buys persistence for vanished rows (a real benefit) at the price of a stateful minting process in the critical nightly path and a survivor policy that loses hours silently. Not recommended as a whole; two ideas are worth stealing (below).

### Option 3: Lineage + Snapshot keys (trigger-computed, `key_version`)

**Id.** Two 16-hex keys, both pure functions of stored columns, minted by a `BEFORE INSERT OR UPDATE` trigger on `stg_timer_activities` (so Python can never disagree with Postgres):

- `lineage_key = left(md5(lower(user_email)|epoch(start_time)|project_did|coalesce(task,'')),16)`
- `snapshot_key = left(md5(lineage_key|coalesce(epoch(end_time),'null')|to_char(round(duration_min,2),'FM9999990.00')),16)`

Form payload `v2:<lineage16>:<snapshot16>` (36 chars). Legacy `id:<md5>` and bare md5 still parse as key_version 1. `entry_id` stays the UNIQUE column (new rows store `entry_id = snapshot_key`); real identity is (lineage_key, snapshot_key) with a partial UNIQUE on key_version = 2.

**Remove/Edit resolution.** REMOVE on (L,S) = drop every raw row of L whose snapshot_key = S (which already absorbs precision tails and site rewrites) and nothing else; NULL-end snapshots match only NULL-end rows (241 semantics kept). EDIT on (L,S) = an assertion about the LINEAGE: keep S updated to X, drop every other snapshot of L; if S vanished, materialise a virtual row for L (Step 4); latest `corrected_at` wins per lineage; correction beats removal per lineage. Lookups are indexed point reads on `snapshot_key`; v2 refs whose snapshot is absent are stored as latent, never skipped. Duplicate groups: label match by snapshot_key; one `_pick_survivor` (drop end NULL, <0.5 or >720 min; one plausible left -> selected; several -> longest plausible; none -> resolved with no survivor and a log line) and one `_reject_snapshots` writer that emits BOTH `rejected_entries` and `entry_removals`. Stragglers never dethrone a selected plausible snapshot.

**What changes.** Migration 242: columns + trigger + indexes on staging/clean; `lineage_key`, `snapshot_key`, `key_version`, (`scope` on removals) on the three action tables with partial UNIQUE; `duplicate_reviews.lineage_key`/`selected_snapshot_key` and per-element keys in JSON; backfill of all 401,786 staging rows and every action row from natural COLUMNS in one transaction. Migration 243: new rebuild body with 218/241 preflight. `transform.py`, `extract_timer.py`, both forms, both `.gs` triggers, `pipeline-timer.yml`, and `daily_notifications.last_sent_entry_ids` are untouched (no resend rebase). Code: ~12 functions in `timer_correction_review.py`, recipes repointed, 7 ad hoc md5 scripts retired with a RuntimeError guard. Rebuild: anti-joins become indexed (L,S) equality; new predicate (d) correction collapse gated to key_version 2; new predicate (e) ghost purge (0-min NULL-end rows beside a completed sibling, 15 today); Step 5 checks siblings in RAW and drops the lineage-wide removal shield.

**Seven scenarios.**

| # | Behaviour |
|---|---|
| 1 | lineage_key unchanged; NULL-end snapshot matches only NULL-end rows, so the 26 legacy NULL-end removals stay inert per spec 6; running rows have no buttons. No loss. |
| 2 | Same lineage, different snapshot keys; Remove B drops B and its re-emissions only. Exact by construction (the key IS the displayed end second + 2-dp duration). Exact twins (53/60d) share a key and go together, which the email cannot distinguish either. |
| 3 | Remove real -> real gone AND Step 5 drops the runaway (raw sibling exists, nothing protects it), so a lineage can legitimately go to zero. Remove runaway -> only runaway. Edit real -> one corrected row. Late runaway straggler rejected as implausible, selected row untouched. |
| 4 | Trigger recomputes both keys on every insert. Correct. |
| 5 | round(duration,2) + epoch seconds; JSON matched by snapshot_key. Correct. |
| 6 | 'null' snapshot in the lineage; predicate (e) purges it beside a completed row; a legacy NULL-end removal can never be read as a decision about the completed row. Correct. |
| 7 | Clicked snapshot rejected exactly; survivor via `_pick_survivor` (plausible then LONGEST when several remain). Still a heuristic, but the member's click is honoured and latest-end is gone. |

**Migration.** Inside 242: stamp staging; corrections (1,582) and removals (8,292) get key_version 1 and keys from their own columns (`original_duration_min` for corrections); additions (98) likewise; `duplicate_reviews` (3,719) JSON rewritten with per-element keys; partial UNIQUE created LAST so legacy collisions cannot block. Effect: removals inert because of a tail or a site rewrite (16 site-context rows in 60d alone) become LIVE again (expected low hundreds, must be listed in the preflight); removals inert because end moved (17/60d) stay inert; the 544 orphaned corrections keep virtualising exactly as today (key_version 1 = no collapse). Rollback: 242 drop columns/trigger/indexes; 243 re-apply the 241 body (new action rows still carry natural-key columns, so the OLD rebuild keeps matching them).

**Effort.** 9-10 engineer-days plus two shadow-rebuild nights. **Risk.** Medium-high as written, because 243 bundles three hours-affecting changes (ghost purge, Step 5 raw-sibling/no-shield, v2 correction collapse) plus re-enlivened removals under a single diff gate. Medium if 243 is split (see recommendation).

**Judge scores.** Correctness 8/10 (no fatal flaw; residual = longest-plausible for 3+ and stale groups, and the invisible Step 5 side effect). Ops risk 8/10 (smallest surface; no new endpoint, registry, lock or rebase; incremental; only deduction is the bundling). Technician 6.5/10 (per-row Remove buttons remain; the id has zero human meaning; confirmation-email join switch was marked optional, which is exactly the surface a tech uses to see what happened). Rankings: 1st, 1st, 2nd.

### Option 4: Lineage Actions (one row per timer; Keep / Remove all / Set duration ledger)

**Id.** `lineage_id = 'tl1_' || left(md5(lower(user_email)|epoch(start_time)|project_did|coalesce(task,'')),16)` and `version_key = 'vopen' | 'v' || epoch(end_time)`, both GENERATED ALWAYS STORED columns on staging, clean and the three legacy tables (no Python involvement). Form payload encodes the action inside Entry ID: `tl1_...` = Remove all (Remove form) or Set duration (Correct form); `tl1_...#keep=v1787359303` = Keep that version; `#from=v...` remembers what an Edit was looking at. Entry Details becomes self-describing and sufficient to recompute the lineage: `<email> | 2026-08-20 15:20:09 ET | <project> | <task> | KEEP ended 15:21 (0.1h)`.

**Remove/Edit resolution.** Everything goes into one append-only ledger `app_timer.lineage_actions` (actions keep_version / remove_all / set_duration / remove_version, `superseded_at`/`superseded_by`, partial UNIQUE on active lineage-level action). Last submission wins across both forms (replaces "correction beats removal"). Rebuild consumes the ledger by lineage_id: remove_all drops the lineage; keep_version(v) drops every other version INCLUDING ones that arrive later (so straggler reconcile is deleted); set_duration keeps one row at the member's number; remove_version is legacy/snapshot-scoped. `_resolve_duplicate_for_action`, `_reconcile_resolved_group_stragglers`, `_resolve_stale_response`, `_uncovered_rows` are deleted outright. Un-acted pending groups show the provisional row (longest <=720) instead of latest end.

**What changes.** Migration 242: two IMMUTABLE functions, generated columns on five tables (breaks the live rebuild's positional `INSERT ... SELECT t.*` at 241 line 63, so 242 and 243 must ship in one window), ledger table + view + backfill + freeze trigger on the legacy tables. Migration 243: rebuild drops from ~215 to ~120 lines, adds Step 6 ghost drop. Email: one row per lineage; multi-version lineages list versions with a Keep button each plus Set duration / Remove all; no per-version Remove anywhere. Forms structurally unchanged (copy tweaks only); `read_form_responses` also reads the Timestamp column. Resend dual-read window for 7 days, no rebase script.

**Seven scenarios.**

| # | Behaviour |
|---|---|
| 1 | version flips vopen -> v<end>, resend fires; lineage-level actions follow into the completed row (legacy NULL-end corrections now SET the completed row's duration via the provisional fallback, a semantic change). |
| 2 | One row, two versions, Keep each; keep_version keeps exactly that end second. Exact and member-driven. |
| 3 | No per-version Remove, so the incident path is deleted rather than patched. Un-acted: provisional (longest <=720) shown; Step 5 still drops >720 beside <=720. Best of the four. |
| 4 | Generated columns, byte-identical nightly. Correct. |
| 5 | Duration in neither key. Correct. |
| 6 | vopen beside a settled version deleted by Step 6 (0 hours). Correct. |
| 7 | The action does not exist; Keep is the primitive. |

**Migration.** Corrections -> set_duration (latest `corrected_at` active per lineage; the 544 orphans attach to the surviving lineage row); non-REVERTED removals -> remove_version (snapshot-scoped, the 26 NULL-end map to vopen and stay inert; 794 inert stay inert); resolved duplicate groups -> keep_version from `selected_entry`; pending groups get no ledger row. Verify SQL with the three named incidents (prince 2026-07-29 22.9h and 2026-08-19 21.9h x2 absent; 2026-08-10 restored 22.6h present). Legacy tables frozen by trigger.

**Effort.** 8-9 engineer-days. **Risk.** Medium-high: hours WILL move on apply (pending groups switch to provisional; set_duration collapses lineages), and rollback to the 241 body silently discards every action taken since cutover because they exist only in the ledger.

**Judge scores.** Correctness 7.5/10 (best member-driven semantics; but `#keep=` percent-decoded by a mail client or link proxy, or trimmed by a member, turns a KEEP into REMOVE ALL, the most destructive silent-loss path of the four; last-wins lets a stale Remove click delete a fresh Edit). Ops risk 4/10 (all-at-once cutover coupled by generated columns; no rollback for new actions; email, form semantics, dedup workflow, resend key, confirmations and two hours defaults all change the same night). Technician 8/10 (94% of rows look identical; duplicated timers become ONE decision; Entry Details readable by Jamil without decoding; undo exists for the first time; deduction: Keep opens a form titled Remove with a Reason field). Rankings: 2nd, 4th, 1st.

**Blunt take.** The right end state for the technician, the wrong way to get there. The affordance is the best idea in the whole panel; the ledger-only cutover, the generated-column coupling, and the `#` fragment are avoidable engineering choices, not consequences of the idea.

### Score summary

| Option | Correctness | Ops risk | Technician | Sum |
|---|---|---|---|---|
| 1 Swift-native id | 7 | 7 | 5 | 19 |
| 2 Registry | 6.5 | 5 | 6 | 17.5 |
| 3 Lineage + snapshot keys | 8 | 8 | 6.5 | 22.5 |
| 4 Lineage actions ledger | 7.5 | 4 | 8 | 19.5 |

## 4. Recommendation

**Build Option 3 as the identity, deliver Option 4's affordance on top of it, and run Option 1's entity fetch in shadow as evidence. Drop Option 2.**

Plain-terms why:

- Option 3 is the only design that fixes identity with zero new moving parts in the nightly path: no second endpoint, no auth0 mapping, no registry, no advisory lock, no resend rebase, no form change. Keys are computed by a trigger from four stable fields (user_email + start_time had 0 in-place edits in 60 days; project_did and task had 0 changes; site fields had 75 rewrites and are excluded; end/duration are excluded), and a second key names the exact snapshot the member saw at seconds/2-dp precision. It won two of three lenses and was second in the third.
- Option 4 wins the technician lens by a wide margin for one reason: it deletes the per-version Remove button, and with it every code path that derives a survivor from a removal. That is the actual complaint ("the one they remove is not what it is") and neither Option 1 nor 3 touches it. But its identity plumbing (generated columns, ledger-only writes, `#` fragment, last-wins) is what dragged it to 4/10 on ops risk. Those parts are separable: render one row per `lineage_key` with Keep per `snapshot_key`, encode the action with a non-fragment delimiter, and write into the Option 3 action tables. Nothing about the affordance requires a ledger or generated columns.
- Option 1's native id is the only non-heuristic signal for "which twin is real" (creation time ~= own end = real stop; creation == start with far-later end = left open until the next timer). We should collect it, but not bet identity on an endpoint probed for one project over eight days, and not pay the pairing/mapping risk on the critical path. As an additive shadow table it costs nothing at cutover and gives the backtest that settles the survivor default with data instead of argument.
- Option 2 is rejected: frozen replay-assigned ordinals are irreversible, a RAISE inside the transform makes a registry bug a pipeline outage, a partial stamp silently thins clean for a night, and its shortest-plausible default loses hours in DRMC without a member in the loop. Steal two things from it: record an action against a snapshot that has already vanished from raw (Option 3 already does this as "latent"), and when 2+ plausible snapshots remain after a Remove, leave the group pending and ask the member instead of crowning one.

### Phased rollout

**Phase 0: keys and shadow, zero behaviour change (week 1, ~3 days).**
- Migration 242 (list `swift_api_pipeline/migrations/` first; 241 is the latest known): `lineage_key`, `snapshot_key` on `stg_timer_activities` (trigger + indexes) and `_clean` (columns appended in the SAME positional order on both tables, because the live rebuild does `INSERT ... SELECT t.*`); `lineage_key`, `snapshot_key`, `key_version`, `scope` on the three action tables with the partial UNIQUE created last; `duplicate_reviews` lineage/selected keys and per-element JSON keys; full backfill from natural COLUMNS (never from `entry_id`). Preflight numbers written into the migration header: legacy (L,S) collisions per table; count and list of inert removals that would become live; `duplicate_reviews` elements that fail to parse (must be 0). Apply nothing to the rebuild yet.
- Option 1 shadow: `data_raw.raw_timer_entities` (PK swift_timer_id, ETag, decoded `swift_created_at`) fed by a second extractor pass at pageSize 500; `attach` into a nullable `swift_timer_id` column on staging only; watcher line for match rate. Also probe one day per month back to 2026-02 for every project with project_number >= 13 and record reach in `reference/README.md`.
- Watcher checks: current-month staging rows with NULL keys = 0; Python-vs-SQL key parity on 1,000 sampled rows (from Option 2).
- DRMC hours do not move. Rollback = drop columns.

**Phase 1: identity-correct apply (week 2, ~4 days).**
- `timer_correction_review.py`: `_make_lineage_key` / `_make_snapshot_key` / `_make_entry_ref` / `_parse_entry_ref`; emails prefill `v2:L:S`; Entry Details gains the ET start time to the second; `read_form_responses` dedups on (L,S); `lookup_entries_by_snapshot` replaces the md5 full scan for v2 refs; upserts revise a legacy row in place through update-then-insert; single `_reject_snapshots` writer used by member action, straggler reconcile and `auto_resolve_stale`; `_pick_survivor` per the rule below; stragglers never dethrone; confirmation classification switched to (L,S) joins (NOT optional, the technician lens is right that this is where the member learns what happened). Retire the 7 ad hoc md5 scripts, repoint the 3 recipes and 2 exports.
- Migration 243a: rebuild anti-joins switched to indexed (L,S) equality ONLY. Expected diff and nothing else: legacy removals re-enlivened by tail/site normalisation (the listed low-hundreds set) and precision-only twin pairs collapsing 2 -> 1 (about 26 per 60 days). Row-level diff by cause, Jamil eyeballs before apply. Because new action rows still carry natural-key columns, rolling back to the 241 body loses nothing.

**Phase 2: the three semantic changes, each its own gate (week 3, ~2 days).**
- Migration 243b: ghost purge predicate (e) (15 rows, 0 hours) and Step 5 raw-sibling check with the lineage-wide removal shield replaced by snapshot-level shields (corrections on (L,S); `selected_snapshot_key`). Expected diff: currently shielded runaways beside a removed real session drop out; list them by (user, date, hours).
- Migration 243c: correction collapse predicate (d), key_version 2 only. Expected diff at go-live: 0 rows. Whether to extend it to the 544 legacy orphans is Jamil's call after the quantified diff.

**Phase 3: the Option 4 affordance (week 4, ~3 days).**
- Email renders one row per `lineage_key`; single-snapshot lineages look exactly like today; multi-snapshot lineages show versions with end time and hours, a Keep per version, plus Set duration and Remove all. No per-version Remove button.
- Encoding: `v2:<L>:<S>:keep`, `v2:<L>::all`, `v2:<L>:<S>:set`. No `#`, no fragment; a bare or malformed ref is logged for manual review and is NEVER interpreted as Remove all. Keep and Remove all both ride the Remove form; form description copy updated (5 minutes in the Forms UI).
- Keep writes `selected_snapshot_key` on the group plus `_reject_snapshots` for every other snapshot, and the rebuild honours `selected_snapshot_key` for snapshots that arrive later (Option 4's "keep excludes future versions" without a ledger).
- Keep "correction beats removal" on the same lineage as today unless Jamil chooses last-wins (decision below).
- Optional if the Phase 0 probe holds up: pre-label versions in the Keep list using `swift_created_at` ("likely real" / "likely stuck timer").

**Phase 4: settle the un-acted default with data (later, ~1 day).**
- Backtest 30 days of `swift_created_at` against the groups `auto_resolve_stale` resolved. If creation-time-near-own-end identifies the real session with high agreement, switch the un-acted default from "longest plausible" to "push-id creation heuristic, then longest plausible". If the entity endpoint proves reliable over months, `swift_timer_id` can become the stored join key for legacy-vanished rows; nothing above has to change for that.

Total: roughly 13 engineer-days across four weeks, versus 9-10 for Option 3 alone; the extra buys the affordance and the evidence column.

### The explicit survivor rule for a duplicate group

Applied everywhere (member action, straggler reconcile, `auto_resolve_stale`, rebuild provisional row), through one function:

1. The member's click is the decision. A Remove on snapshot S rejects S and only S. A Keep on S selects S and rejects every other snapshot of the lineage, including any that appears on a later night. A Set duration collapses the lineage to one row at the member's number.
2. Implausible snapshots are never survivors: `end_time IS NULL`, `duration_min < 0.5`, or `duration_min > 720` when a <=720 sibling exists in RAW.
3. After a Remove, if exactly one plausible snapshot remains, it is selected. If two or more remain, the group stays pending, the clean table shows the provisional row, and the member gets the duplicate-review prompt again. No code path crowns a survivor from a removal.
4. Provisional row for pending groups and default for stale un-acted groups: longest plausible (today's `auto_resolve_stale` policy, errs toward keeping hours, no DRMC drop on cutover). Never latest `end_time`. Shortest is rejected until Phase 4 data justifies a change.
5. A late-arriving snapshot never displaces a selected snapshot. If the selected one is itself implausible and the newcomer is plausible, the group reopens to pending rather than auto-switching.
6. Every rejection writes an `entry_removals` row keyed (L,S) AND the `rejected_entries` mirror, so the rebuild has one exclusion source and the two cannot drift.

## 5. Decisions Jamil needs to make

1. Lineage = `user_email + start second + project_did + task`, site excluded (recommended; the alternative keeps site rewrites as a lost-action class, 21 in the last 60 days).
2. Snapshot-scoped removals on NULL-end rows stay inert after completion (spec section 6 / migration 241 kept; recommended). Option 1's pass C would reverse this; say no unless you want a 0-min removal to be able to delete a real session.
3. Survivor rule as written in section 4, specifically: longest plausible as the un-acted default (status quo) vs shortest (Option 2) vs wait for push-id evidence.
4. Edit semantics: an Edit collapses the whole lineage to the corrected row (recommended, it is what a tech means by "set this to 2.0"), gated to new (key_version 2) corrections at go-live; extend to the 544 legacy orphans only after the quantified diff.
5. Step 5 behaviour change: removing the only plausible session also drops the runaway (lineage goes to zero hours) instead of the runaway surviving under the old shield. Recommended, but it must be visible in the confirmation email.
6. Phase 3 affordance: approve one row per timer with Keep / Remove all / Set duration and no per-version Remove; approve the Remove form's description copy.
7. Cross-form priority: keep "correction beats removal" (recommended, order-independent) or adopt Option 4's last-submission-wins (gives undo, but a stale click on an old email can delete a fresh edit).
8. Whether to fund the Option 1 shadow extractor in Phase 0 (recommended, ~1.5 days, additive) and the historical-reach probe.
9. Whether byte-identical twins with distinct Swift ids should count twice in clean (recommended: no change, still collapsed).
10. Migration split: three gated rebuild migrations (243a/b/c) rather than one; accept the longer calendar.

## 6. Open questions / risks

- Entity endpoint reach: probed only 2026-08-17..24 on TS19. Historical depth, silent-truncation threshold, and behaviour on non-Tech-Ops projects are unknown. Phase 0 probe answers this before anything depends on it.
- Positional `t.*` in the live rebuild (241 line 63): the new columns must be appended in identical order on `stg_timer_activities` and `_clean`, and the preflight must sweep `pg_get_functiondef` across `data_staging`, `analytics`, `app_hr` (ops report migration 240, incremental/targeted loaders) for other `SELECT *` consumers of the two timer tables.
- Same-second, same-task, different-site sessions (4 of 11 collisions in 22,704 user+start pairs over 60 days) merge into one lineage; a Remove is unaffected, an Edit or Keep collapses both. Rare, documented, not solved.
- Swift task rename or email alias remap re-keys a lineage (0 observed in 60 days). Actions go latent, not lost; add the watcher line "active actions younger than 90 days with no raw lineage".
- Swift true deletions (25 keys in August, one user) keep any attached action inert forever; "inert removals" never reach zero and the confirmation email must stop rendering them as REMOVED (fixed by the (L,S) classification join).
- Re-enlivened legacy removals (tail/site-inert rows that start matching again) will change clean hours for a few dozen user-days at 243a; DRMC variance for those days moves. The row-level diff must list them and Jamil should eyeball before apply.
- Python/SQL key parity: keys are minted in SQL; Python mirrors exist for tests and for recomputing from Entry Details. ROUND_HALF_UP vs PG `round`, tz-naive datetimes, and task whitespace are the drift points; the 1,000-row parity test and the trigger-only minting are the guards.
- Legacy responses in the two Google Sheets keep using the md5 scan and the details fallback for a 90-day grace window; same-day same-task twins from pre-cutover emails remain ambiguous and are skipped.
- Duration float tails: no longer load-bearing for identity after Phase 1, but 96.1% of rows still carry them; rounding at `transform.py:948` is a separate, cosmetic change and should not be bundled.
- Phase 3 encoding: verify once through the LIVE forms from jamil.mendez@ontel.co only that a 40-char `v2:...:keep` value survives prefill -> submit -> sheet as text and that no field validation rejects it.
- Rebuild runtime: predicates get simpler, not longer, but the 300s statement timeout and the watcher timing must be recorded before/after each of 243a/b/c.
- Procedural: Cloudflare WARP on for DB steps; `premerge-review` before merge; README/CHANGELOG, `DATA_PIPELINE_DOCUMENTATION.md:187-188,278`, `docs/specs/timer-daily-task-summary.md:48`, the module docstring at `timer_correction_review.py:21`, and `agent.schema_metadata` all describe `entry_id` as md5 of the full natural key and must change in the same PR.