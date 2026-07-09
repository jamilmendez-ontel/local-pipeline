# Employee Email Aliases (Person Resolution) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Emp ID the person anchor warehouse-wide by introducing a dual-source email alias table, so email changes (marriage, surname migration) never break reports↔timers linkage, approver attribution, or approver-group routing.

**Architecture:** `reference.ref_employee_emails` maps every email a person has ever had to their emp_id (+ Swift auth0 id when known). Fed from BOTH directions, first-one-wins (user decision 2026-07-09): (a) a trigger on `ref_employees` captures roster email updates the moment HR's sheet sync applies them (critical: sync_employees.py UPDATEs email in place, destroying the old value), and (b) a harvest function reads `submittedBy.email` + `aliasFor.id` (auth0) from raw daily-report task payloads every pipeline run, so a member submitting under a new Swift email registers within ~5 minutes. `mv_timer_day_rollup` is rekeyed to a resolved person key; `v_hr_report_review` joins timers by emp_id; approver attribution and group functions resolve emails through aliases.

**Tech Stack:** Postgres (Supabase cloud `voqfjfngdpcvevbkikud`), pg_cron, Python pipeline (local-pipeline).

## Global Constraints

- Migrations `167`, `168`, `169` in `local-pipeline/swift_api_pipeline/migrations/`. **Re-list the dir for the next free number before each file** (166 was taken by COP forecast mid-day).
- Standards: every object via migration; snake_case; `reference` schema for the lookup; semantic metadata rows in `agent.schema_metadata`; revoke anon/authenticated, grant service_role (mirror grants of existing `ref_*`/analytics objects).
- **Do NOT drop `analytics.v_hr_report_review`** (functions `hr_review_page` RETURNS SETOF it; `hr_review_*` bodies read it). MV swap must be: create new MV → CREATE OR REPLACE the view onto it → drop old MV → rename new MV to the old name.
- Emails stored/compared lowercased.
- Parity bar: with today's data (no email change has happened yet), person-keyed rollup must reproduce the email-keyed rollup 1:1 (same row count, same summed union_min, review-view timed/variance columns unchanged).
- Timer corrections keys `(project_did, user_email, start_time)` are raw-export record keys: OUT of scope, do not touch.
- App sign-in allowlist (`app_hr.hr_app_user`) is account identity: OUT of scope, note as follow-up.

---

### Task 1: Migration 167 — alias table, seed, roster trigger, Swift harvest fn

**Files:** Create `local-pipeline/swift_api_pipeline/migrations/167_employee_email_aliases.sql`

**Produces:**
- `reference.ref_employee_emails(emp_id text, email text, auth0_id text, source text, first_seen timestamptz, last_seen timestamptz, note text, PK (emp_id, email))` + index on (email)
- Trigger `ref_employees_email_alias` on `reference.ref_employees` AFTER INSERT OR UPDATE OF email → upsert alias (source 'roster')
- `reference.harvest_employee_email_aliases(p_run_id uuid default null) returns integer` (plpgsql): unwrap `data` jsonb (stored as jsonb string: `case when jsonb_typeof(data)='string' then (data #>> '{}')::jsonb else data end`), rows `source_type='task'`, optional run filter; join `data_staging.stg_daily_reports USING (task_did)` for emp_id; upsert `(emp_id, lower(submittedBy.email), submittedBy.aliasFor.id, 'swift')`, `ON CONFLICT (emp_id,email) DO UPDATE last_seen=now(), auth0_id=coalesce(excluded,existing)`; return rowcount via GET DIAGNOSTICS.
- Seed: `INSERT ... SELECT emp_id, lower(email), 'roster_seed' FROM reference.ref_employees WHERE email IS NOT NULL ON CONFLICT DO NOTHING`, then one full harvest call `SELECT reference.harvest_employee_email_aliases(NULL)` (backfills from all raw history).
- Metadata rows for table + function.

- [ ] Preflight: next free number is 167; no object named ref_employee_emails exists; capture baselines: `select count(*), count(distinct email) from reference.ref_employees where email is not null` and `select count(distinct lower(d->'submittedBy'->>'email'))` over unwrapped raw task rows.
- [ ] Write + apply via Supabase MCP `apply_migration`; verify: alias rowcount >= roster distinct emails; harvest return > 0; spot-check one employee has roster row and (if they submit reports) a swift row with auth0_id populated; trigger test: update a test row's email in a transaction and ROLLBACK after confirming alias row appears.
- [ ] Commit migration to local-pipeline main, push.

### Task 2: Pipeline harvest hook

**Files:** Modify `local-pipeline/swift_api_pipeline/extract_daily_reports.py` (after stg loads, near the Step-5 reconcile), and its rolling wrapper only if it doesn't reuse the same load path.

- [ ] Add a step: `db.execute("SELECT reference.harvest_employee_email_aliases($1::uuid)", RUN_ID)` wrapped in the file's `retry_db(...)` idiom, logged as "Harvesting email aliases", guarded so a harvest failure does NOT fail the run (log warning) — alias freshness is best-effort per run, the next run self-heals.
- [ ] Run the rolling pipeline once locally (or wait for the next scheduled run) and confirm the step logs + `last_seen` advances for a current submitter.
- [ ] Commit + push.

### Task 3: Migration 168 — person-keyed timer rollup + review view swap

**Files:** Create `local-pipeline/swift_api_pipeline/migrations/168_timer_rollup_person_key.sql`

New MV (same interval-union logic as 145, source unchanged) with resolution CTE:
```sql
resolved AS (
  SELECT iv.*, (SELECT ea.emp_id FROM reference.ref_employee_emails ea
                WHERE ea.email = lower(iv.user_email)
                ORDER BY ea.last_seen DESC LIMIT 1) AS emp_id
  FROM iv
)
-- person_key = COALESCE(emp_id, 'email:' || lower(user_email))
```
Grain/groupings switch from `(user_email, work_day)` to `(person_key, work_day)`; output columns `person_key, emp_id, work_day, union_min, entry_count, open_count, first_start`; unique index `(person_key, work_day)`.

Swap sequence (single migration, per Global Constraints):
1. `CREATE MATERIALIZED VIEW analytics.mv_timer_day_rollup_new ...` + unique index.
2. `CREATE OR REPLACE VIEW analytics.v_hr_report_review` = the live def (fetched via pg_get_viewdef at execution) with ONLY these changes: `tm` join → `ON tm.person_key = b.emp_id AND tm.work_day = b.work_date`; `th` lateral → `WHERE m2.person_key = b.emp_id`; every `tm.user_email IS NOT NULL` → `tm.person_key IS NOT NULL`; `th.user_email IS NOT NULL` → `th.person_key IS NOT NULL`. Column list/order unchanged (hr_review_page return type untouched).
3. `SELECT cron.unschedule('refresh_mv_timer_day_rollup');` then `DROP MATERIALIZED VIEW analytics.mv_timer_day_rollup;`
4. `ALTER MATERIALIZED VIEW analytics.mv_timer_day_rollup_new RENAME TO mv_timer_day_rollup;` + rename index to `mv_timer_day_rollup_pk`; re-`cron.schedule` the same job name/SQL.
5. New view `analytics.v_unmatched_timer_emails`: rollup rows `person_key LIKE 'email:%'` in last 30 days (email, last day, entry count) — the watcher surface.
6. Metadata updates.

- [ ] Baselines BEFORE applying: old MV `count(*)`, `sum(union_min)`, and 5 sample (email, day) rows; review-view `count(*) where timed_hours is not null` and `sum(timed_hours)`.
- [ ] Apply; parity: new MV same `sum(union_min)`; resolved rows = old rows for roster emails; unmatched count = old unmatched; review-view timed/variance aggregates identical; `v_unmatched_timer_emails` returns plausible small set.
- [ ] `/hr/reports` prod spot-check (timer hrs column populated, variance unchanged on sample rows). Commit + push.

### Task 4: Migration 169 — approver attribution + group functions via aliases

**Files:** Create `local-pipeline/swift_api_pipeline/migrations/169_approver_alias_resolution.sql`

- `v_daily_report_approvals` (156 overlay): fetch live def; the approver-name lateral `WHERE re2.email = la.approver_email` becomes alias-first with email fallback:
  `WHERE re2.emp_id = (SELECT ea.emp_id FROM reference.ref_employee_emails ea WHERE ea.email = lower(la.approver_email) ORDER BY ea.last_seen DESC LIMIT 1) OR lower(re2.email) = lower(la.approver_email)` (keep existing ORDER/LIMIT shape of the lateral).
- `analytics.approver_groups_for_email(p_email)` (160) + `analytics.approved_members_for_email` (161): the `me`/user lookup `WHERE lower(email) = lower(p_email)` becomes: resolve `p_email` → emp_id via aliases, match directory by emp_id, fallback `lower(email) = lower(p_email)`. Fetch 161's live body before editing; keep 160's collision guard and 180-day window verbatim.
- Metadata description updates for all three.

- [ ] Baselines: `approved_by` non-null count in v_daily_report_approvals; `approver_groups_for_email` output for the 4 known approvers (tan/orville/hajie/merjien emails).
- [ ] Apply; parity: identical counts/outputs (no email has changed yet, so alias resolution must be a no-op today). Commit + push.

### Task 5: Docs + bookkeeping

- [ ] WORK_LOG session entry (ET range); memory: new topic file `project_employee_email_aliases.md` + MEMORY.md line + cross-link from ontel-people/hr topics; Obsidian log line (local-pipeline note) + push vault.
- [ ] Completion summary MUST name deferred items: watcher ALERTING hookup (view exists, alert wiring into roster-gap-watcher/pipeline-guardian later), app sign-in email changes (account op), timer corrections keys untouched, roster-vs-swift disagreement view (YAGNI'd).

## Self-Review
- Dual-source requirement (user 2026-07-09): Task 1 trigger (roster-first) + Task 1 fn/Task 2 hook (swift-first) both feed one table; resolution reads the table only → first writer wins automatically. Covered.
- No-drop constraint on the review view honored via create-new/replace/drop-old/rename.
- Parity bars defined per task with pre-apply baselines.
- Risk: pg_get_viewdef-based edits — anchors specified exactly; executor has live defs in context.
