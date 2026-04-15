# Pipeline Guardian Agent — Design

**Status:** Approved
**Date:** 2026-04-15
**Author:** Jamil + Claude brainstorm session
**First auto-remediation target:** asset_tasks baseline-inflation bug (see `project_asset_tasks_baseline_bug.md`)

---

## 1. Problem & Goals

### Problem
Pipeline failures currently require Jamil to open Claude Code, diagnose, and manually execute fixes. This happened tonight (2026-04-15) when the asset_tasks pipeline was interrupted by a PC shutdown, leaving stale rows in `data_raw.raw_asset_tasks` that tripped the pipeline's safety check on the next run. Tonight's session took ~40 minutes of manual effort to diagnose, clean up stale data, and re-run. Similar manual interventions happen ~2-3 times per week across the pipeline fleet (53 failed runs + 12 stuck runs in pipeline history).

### Goals
1. **Detect pipeline failures and missing runs automatically**, without Jamil checking manually
2. **Auto-fix** the handful of well-understood, reversible failure modes (stale raw data, stuck `running` status, orphaned indexes)
3. **Propose fixes** for bigger actions (re-run a pipeline) and wait for approval via natural-language email reply
4. **Escalate** novel / risky patterns with full context so Jamil can intervene
5. **Work whether Jamil's PC is on or off** — run entirely in the cloud (GitHub Actions), with the ability to trigger local pipelines when the PC comes back online

### Non-goals
- Replacing Jamil's judgment on novel failures — escalate-only patterns explicitly stay escalate-only
- Fixing pipeline bugs (code changes) — agent only remediates data/state issues, not source code
- Multi-user approval workflows — single user (Jamil) owns all approvals

---

## 2. Architecture Overview

```
┌──────────────┐    email     ┌──────────────┐   repo-dispatch   ┌────────────┐
│   Pipeline   │─────────────>│  Apps Script │──────────────────>│    GHA     │
│  (local/GHA) │              │   Watchers   │                   │   Runner   │
└──────────────┘              └──────────────┘                   │            │
                                      ▲                          │   Agent    │
┌──────────────┐    reply     ┌──────────────┐                   │  (Python + │
│     You      │─────────────>│  Gmail Inbox │                   │  Claude SDK│
└──────────────┘              └──────────────┘                   │  + DB +    │
      ▲                                                          │  GitHub)   │
      │              status/diagnosis/action emails              │            │
      └──────────────────────────────────────────────────────────┤            │
                                                                 └────────────┘
                                                                       │
                              ┌───────────────────────────┐            │
                              │      Supabase DB          │<───────────┘
                              │ - pipeline.pipeline_runs  │     query/fix
                              │ - agent.monitor_state     │
                              │ - data_raw.* (cleanup)    │
                              └───────────────────────────┘
```

**Runtime environment:** GitHub Actions workflow in the `local-pipeline` repo. Triggered by `repository_dispatch` events from Apps Script. Runs a Python script using the Anthropic SDK for Claude reasoning, plus the pipeline's existing `db.py`, `pipeline_notifier.py`, `gmail_client.py` modules.

**Why GHA over local Claude Code:** Works whether the PC is on or off; matches the existing Apps Script → GHA pattern used for gmail pipelines, timer duplicate resolve, and timer correction apply; trivially scales when `asset_tasks_extract` moves from local to cloud.

---

## 3. Triggers

Three Apps Script triggers, each firing a `repository_dispatch` to wake the agent in GHA:

### Trigger 1: Pipeline failure email (event-driven, ~sub-minute latency)
- Gmail watcher on `subject:"Pipeline FAILED"` (the subject pattern sent by `pipeline_notifier.py`)
- Fires `repository_dispatch` with `event_type: guardian-failure`
- Payload: `{trigger: "failure", pipeline_name, run_id, email_thread_id}`
- **Account configuration**: Apps Script runs under `jamil.mendez@nanoninth.com` (matching existing Apps Script triggers per memory). `pipeline_notifier.py` will be updated to add `jamil.mendez@nanoninth.com` to the recipient list so failure emails arrive in the account the Apps Script monitors. Alternative: script can read from Sent folder since the notifier already sends from nanoninth.

### Trigger 2: Time trigger every 5 min (periodic check)
- Apps Script time-based trigger (reliable per Jamil's feedback memory — GHA cron is unreliable, Apps Script triggers are not)
- Fires `repository_dispatch` with `event_type: guardian-periodic`
- Agent checks for:
  - Hung runs (`status='running'` AND `started_at < now() - 2h`)
  - Missing runs (expected pipeline X didn't start within its grace window)
  - Newly-started runs that were previously flagged as missing (sends "pipeline now running" status email)
  - Pending approvals awaiting reply >12h (sends one-time gentle reminder)

### Trigger 3: Email reply (conversational)
- Gmail watcher on `subject:"[Pipeline Guardian]"` with `in:inbox is:unread`
- Fires `repository_dispatch` with `event_type: guardian-reply`
- Payload: `{trigger: "reply", thread_id, message_id}`

### Idempotency
Agent checks `agent.monitor_state` before acting. Each detection writes a row keyed on `(run_id, pattern_id)`. If the row already exists in state `auto_fixed` or `executed`, the agent skips — no duplicate emails, no double-fixes.

---

## 4. Agent Behavior per Trigger

### Flow 1: `trigger=failure`
1. Load failed run from `pipeline.pipeline_runs` (status, error_message, timing)
2. Gather log context:
   - **For GHA-run pipelines**: fetch last 200 lines of the GHA workflow log via GitHub API
   - **For local pipelines** (asset_tasks, calendar_leave): agent can only see `pipeline_runs.error_message`. To provide richer context, `pipeline_notifier.py` will be extended to include the last 30 lines of the pipeline log in the failure email body; the agent parses this from Gmail. (Enhancement tracked as a deliverable.)
3. Classify failure via detector rules (`detectors.py`) — matches against `agent.known_issues` patterns
4. Based on matched pattern's severity:
   - `auto` → apply fix, send summary email
   - `approve` → email Jamil with diagnosis + proposed action, write `awaiting_approval` state
   - `escalate` → email with Claude-generated explanation + raw log excerpt, write `escalated` state
5. If no pattern matches → treat as `escalate` with `pattern_id='unknown'`

### Flow 2: `trigger=periodic`
1. Query `pipeline.pipeline_runs` for the last 24h of activity
2. Run checks in order:
   - **Hung runs**: status=running >2h → mark as failed, send summary
   - **Missing runs**: cross-reference `agent.pipeline_schedule` for expected runs today → if missing, send `[Pipeline Guardian] Missing run: X` email
   - **Started-after-missing**: if a previously flagged `missing_run` now has a running entry → send "pipeline X is running now" status email to confirm detection
   - **Pending approvals**: rows in `awaiting_approval` state with `created_at < now() - 12h` → send one-time reminder email (tracked by `reminder_sent_at` metadata)
3. If nothing to do → exit silently (no "all clear" emails)

### Flow 3: `trigger=reply`
1. Fetch reply thread from Gmail via `gmail_client.py`
2. Look up pending `awaiting_approval` row in `agent.monitor_state` by `email_thread_id`
3. Call Claude API (`email_parser.py`) with: reply text + pending action context → classify as `approve` / `modify` / `decline` / `instruct`
4. Execute approved action; update `monitor_state` row to `executed` or `declined`
5. Send confirmation email: "Done — here's what I did" or "Acknowledged, not running"

---

## 5. Knowledge Base (18 Failure Patterns)

Seeded from analysis of 53 historical failed runs (pipeline_runs table) + pipeline log scan.

### Auto-fix (5 patterns — reversible, safe)

| pattern_id | Detection | Fix action |
|---|---|---|
| `stale_runs_in_raw_table` | Error contains `Cleanup aborted: new run has X rows but old run has Y` | `DELETE FROM data_raw.<table> WHERE run_id != <current_run_id>`, then email summary |
| `stuck_running_status` | `pipeline_runs.status='running'` AND `started_at < now() - 2h` AND no log activity | `UPDATE pipeline_runs SET status='failed', error_message='Auto-marked by guardian: stuck >2h'` |
| `index_already_exists` | Error matches `idx_raw_\w+ already exists` | `DROP INDEX IF EXISTS ...; CREATE INDEX ...` (sequence re-runs pipeline's own index restore) |
| `dns_blip_at_startup` | Error contains `gaierror` or `getaddrinfo failed` during pool creation | No action — pipeline's own 3x retry in `db.py` handles it; agent just logs the event |
| `swift_api_503_transient` | Log shows retries 1-9 of 10 succeed eventually | No action — pipeline's 10x retry handles it; agent only flags if all retries exhaust |

### Propose + approve (6 patterns — bigger actions)

| pattern_id | Detection | Proposed action |
|---|---|---|
| `statement_timeout_57014` | Error code `57014` | Re-run failing stage (e.g., `--pipeline forms`) |
| `connection_closed_midop` | Error contains `connection was closed in the middle of operation` | Re-run failing stage |
| `partial_extraction` | Error matches `Asset tasks partial failure: <project> failed` | Re-run full pipeline (granular per-project re-run not yet supported) |
| `missing_run` | No run started for pipeline X by grace window | Trigger pipeline (GHA dispatch for cloud, flag file for local) |
| `safety_check_legitimate_block` | After `stale_runs_in_raw_table` auto-fix: previous good run's data was valid | Re-run pipeline (exactly what happened tonight, 2026-04-15) |
| `duplicate_key_raw_table` | Error code `23505` on `raw_*` table | Clean stale run data, re-run |

### Escalate only (7 patterns — needs Jamil)

| pattern_id | Detection | Why escalate |
|---|---|---|
| `oauth_token_expired` | Error contains `invalid_grant: Token has been expired or revoked` | Requires browser re-auth; agent can't do this remotely |
| `out_of_memory` | Error contains `Cannot enlarge string buffer` | Code issue — batch size too large, needs review |
| `module_not_found` | `ModuleNotFoundError` | Dependency missing — needs `pip install` |
| `code_bug_nameerror` | `NameError: name '\w+' is not defined` | Developer fix needed |
| `asyncpg_type_mismatch` | Error contains `'str' object has no attribute 'toordinal'` or similar | Code bug |
| `pgrst106_schema_missing` | Error code `PGRST106` | Config issue — schema not exposed via PostgREST |
| `silent_death` | `status='running'` indefinitely AND last log activity was normal | Rare — escalate, review |

### Unknown pattern handling
If no detection rule matches, agent creates a row with `pattern_id='unknown'` + severity `escalate` + Claude-generated diagnosis. Jamil can add a new rule to `agent.known_issues` afterward based on the learning.

---

## 6. Database Schema

New schema: `agent` (already our-owned per MEMORY.md RULE — only touch our own schemas).

### Table: `agent.monitor_state`

Tracks every detection, decision, and action the agent takes.

```sql
CREATE TABLE agent.monitor_state (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id            UUID REFERENCES pipeline.pipeline_runs(run_id),
    pipeline_name     TEXT NOT NULL,
    pattern_id        TEXT NOT NULL,        -- matches agent.known_issues.pattern_id
    state             TEXT NOT NULL,        -- see state machine below
    severity          TEXT NOT NULL,        -- 'auto' | 'approve' | 'escalate'
    diagnosis         JSONB,                -- Claude's analysis + evidence
    proposed_action   JSONB,                -- action spec (function name + args)
    email_message_id  TEXT,                 -- Gmail message-id for the outbound email
    email_thread_id   TEXT,                 -- Gmail thread for correlating replies
    executed_at       TIMESTAMPTZ,
    result            JSONB,                -- outcome (rows affected, run triggered, etc.)
    reminder_sent_at  TIMESTAMPTZ           -- set when a 12h reminder is sent
);

CREATE INDEX idx_monitor_state_pipeline ON agent.monitor_state(pipeline_name, created_at DESC);
CREATE INDEX idx_monitor_state_state ON agent.monitor_state(state)
    WHERE state IN ('awaiting_approval', 'approved');
CREATE INDEX idx_monitor_state_thread ON agent.monitor_state(email_thread_id)
    WHERE email_thread_id IS NOT NULL;
CREATE UNIQUE INDEX idx_monitor_state_dedupe ON agent.monitor_state(run_id, pattern_id)
    WHERE run_id IS NOT NULL;

-- For missing-run detection (run_id is NULL), dedupe by pipeline+date+pattern via partial unique on metadata
CREATE UNIQUE INDEX idx_monitor_state_missing_run_dedupe ON agent.monitor_state(
    pipeline_name,
    pattern_id,
    (diagnosis->>'expected_date')
) WHERE run_id IS NULL AND pattern_id = 'missing_run';
```

**State machine:**
- `detected` — initial state when a pattern is matched
- `auto_fix_pending` — auto-fix identified; action not yet started (brief, used for dry-run mode)
- `auto_fixed` — auto-fix action completed (terminal)
- `awaiting_approval` — email sent, waiting for reply
- `approved` — user replied with approval; action queued
- `executed` — approved action completed (terminal)
- `declined` — user replied with decline (terminal)
- `escalated` — escalate-only pattern; no action expected (terminal)

Transitions: `detected → auto_fix_pending → auto_fixed` (auto flow); `detected → awaiting_approval → approved → executed` (approve flow) or `awaiting_approval → declined`; `detected → escalated` (escalate flow).

### Table: `agent.pipeline_schedule`

Source of truth for "what runs when" — used to detect missing runs.

```sql
CREATE TABLE agent.pipeline_schedule (
    pipeline_name   TEXT PRIMARY KEY,
    expected_cron   TEXT NOT NULL,            -- e.g. "1 0 * * *" for 12:01 AM
    grace_minutes   INT NOT NULL DEFAULT 15,  -- minutes past scheduled time before flagging
    runner          TEXT NOT NULL,            -- 'local' | 'gha'
    gha_event_type  TEXT,                     -- repository_dispatch event_type for re-trigger (GHA only)
    enabled         BOOLEAN NOT NULL DEFAULT true,
    notes           TEXT
);
```

Seeded with current pipelines:
- `asset_tasks_extract` — local, `1 0 * * *` (12:01 AM ET)
- `calendar_leave` — local, `30 0 * * *` (12:30 AM ET)
- `orgs_projects_extract` — gha, `0 3 * * *` (3 AM UTC = 10 PM ET)
- `timer_extract` / `forms_extract` / `user_priorities_extract` — gha, `1 5 * * *` (12:01 AM ET)
- etc. (full list built from current Task Scheduler + GHA workflows)

### Table: `agent.known_issues`

Editable knowledge base. Adding a new pattern is `INSERT` — no code redeploy.

```sql
CREATE TABLE agent.known_issues (
    pattern_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    detection_rule  JSONB NOT NULL,   -- { "match_type": "regex|sql|log_pattern", ... }
    severity        TEXT NOT NULL,    -- 'auto' | 'approve' | 'escalate'
    fix_action      JSONB,            -- { "function": "clean_stale_runs", "args": {...} }
    description     TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    enabled         BOOLEAN NOT NULL DEFAULT true
);
```

Seeded with the 18 patterns above. `known_issues.yaml` in the repo mirrors this for human editing; a sync script loads YAML → DB.

### Migration: Cleanup historical stuck runs

```sql
-- Migration 046: mark the 12 historical stuck 'running' runs as failed
UPDATE pipeline.pipeline_runs
SET status = 'failed',
    error_message = 'Auto-marked by guardian: historical stuck run (cleaned 2026-04-15)'
WHERE status = 'running' AND started_at < '2026-04-01';
```

---

## 7. Actions & Safety

### Allowed DB actions
- `clean_stale_runs(table, current_run_id)` → `DELETE FROM data_raw.<table> WHERE run_id != <current>`
- `mark_stuck_failed(run_id, reason)` → `UPDATE pipeline.pipeline_runs`
- `recreate_orphan_index(index_name, spec)` → `DROP INDEX IF EXISTS; CREATE INDEX`
- `clean_duplicate_key(table, conflict_run_id)` → targeted delete + sequence reset

### Allowed pipeline trigger actions
- `trigger_gha_workflow(event_type, payload)` → GitHub REST API `POST /repos/.../dispatches`
- `trigger_local_pipeline(pipeline_name)` → write to `pipeline_guardian/triggers/pending.json`; Task Scheduler polls this file every 5 min and launches the pipeline if found

### Communication actions
- `send_email(subject, body, thread_id?)` → via `pipeline_notifier.py` with `[Pipeline Guardian]` subject prefix
- `read_gmail_thread(thread_id)` → via `gmail_client.py`
- `parse_reply_intent(reply_text, pending_action)` → Anthropic SDK call with structured output

### Safety rails
1. **Schema whitelist** — every DB action is routed through a check against `{data_raw, data_staging, analytics, pipeline, agent}`. Attempts to touch `public`, `auth`, etc. raise `PermissionError`.
2. **State-gated execution** — no action runs unless the corresponding `monitor_state` row is in `approved` or `auto_fix_pending` state. Enforced in `actions.py` decorator.
3. **Rate limit** — max 3 automated re-runs per pipeline per 24h window. Tracked via count query on `monitor_state`.
4. **Kill switch** — special row in `agent.monitor_state` with `pipeline_name='_SYSTEM'` and `pattern_id='_DISABLED'`. If present with state `active`, agent exits immediately on every trigger.
5. **Dry-run mode** — env var `GUARDIAN_DRY_RUN=true` logs intended actions without executing. Used throughout Phase 1.
6. **Claude prompt injection defense** — email reply parsing uses structured output (`{intent: "approve|decline|modify|instruct", instructions: "..."}`), not free-text execution. Intents are mapped to pre-defined action functions, not eval'd.

---

## 8. Rollout Plan

### Phase 1: Read-only monitoring (week 1)
- Deploy agent, schema, Apps Script triggers
- `GUARDIAN_DRY_RUN=true` — agent only observes, never acts
- Sends Jamil a daily digest: "here's what I saw, here's what I would have done"
- Goal: validate detection logic, tune knowledge base, catch false positives

### Phase 2: Auto-fix enabled (week 2-3)
- `GUARDIAN_DRY_RUN=false`
- The 5 auto-fix patterns run without approval (stale runs cleanup, stuck status, index recreation, DNS/503 passthrough)
- Approve-level and escalate-level patterns still email only — no auto-execution
- Rate limiting active from day one

### Phase 3: Approval-driven re-runs (week 4+)
- Enable reply parsing for approve patterns
- Jamil's natural-language replies (`"go ahead"`, `"just clean, don't re-run"`) → executed
- Escalate-only patterns remain email-only

### Phase 4 (future): Cloud migration
- When `asset_tasks_extract` moves to GHA (separate project), agent automatically treats it as `runner='gha'` in the schedule
- "PC off" scenarios disappear
- Can raise rate limit since no local resource cost

---

## 9. Deliverables

### Code

**New module:** `pipeline_guardian/` in the `local-pipeline` repo
- `agent.py` — main entry (dispatched by GHA job)
- `detectors.py` — one function per `pattern_id`, returns match + evidence
- `actions.py` — DB cleanups, pipeline triggers, email I/O (with safety rails)
- `known_issues.yaml` — editable KB source (synced to `agent.known_issues` table)
- `email_parser.py` — Claude-powered reply intent classifier
- `schedule_check.py` — missing-run detection logic
- `sync_known_issues.py` — one-way YAML → DB loader
- `README.md` — how it works, how to add patterns, how to disable

**New workflow:** `.github/workflows/pipeline-guardian.yml`
- Handles three `repository_dispatch` event types: `guardian-failure`, `guardian-periodic`, `guardian-reply`
- Secrets: `SUPABASE_PASSWORD`, `NOTIFIER_CREDENTIALS_JSON`, `NOTIFIER_TOKEN_PICKLE`, `ANTHROPIC_API_KEY`, `GH_DISPATCH_PAT`, `GMAIL_GUARDIAN_TOKEN_PICKLE`

**Apps Script:** `scripts/guardian_triggers.gs`
- Gmail watchers (failure emails, reply emails)
- 5-min time trigger for periodic checks

**Task Scheduler integration** (for local pipelines until cloud migration):
- New task `Guardian-LocalTrigger` — runs every 5 min, reads `pipeline_guardian/triggers/pending.json`, launches pipelines listed there, marks them processed
- Configure existing `SwiftPipeline-Nightly` with "Run task as soon as possible after a scheduled start is missed" flag

### Database migrations
- `045_agent_schema.sql` — create `agent` schema + 3 tables (`monitor_state`, `pipeline_schedule`, `known_issues`)
- `046_cleanup_stuck_runs.sql` — mark 12 historical stuck runs as failed
- `047_seed_agent_data.sql` — seed `known_issues` + `pipeline_schedule` from YAML

Note: migration 044 (`044_portal_permissions.sql`) already exists in the repo.

### Documentation
- `docs/superpowers/specs/2026-04-15-pipeline-guardian-design.md` (this doc)
- `pipeline_guardian/README.md`
- Update `MEMORY.md` with pointer to guardian memory file

---

## 10. Open Questions / Future Work

- **Per-project re-run for asset_tasks** — currently if one project fails (e.g., TS16 partial), we propose full re-run. Could add granular per-project re-run later via `--project TS16` flag.
- **Gmail token refresh** — guardian's own Gmail access token needs rotation. Add to existing token rotation reminder pattern in Apps Script.
- **Multi-pipeline failure cascades** — if upstream pipeline fails, downstream might fail too. Agent should detect the cascade and avoid duplicate notifications (propose one fix for the root cause). Track as follow-up enhancement.
- **Observability** — agent decisions should be browsable via the future Operations Portal (`project_portal.md`). Agent writes JSON events that the portal can render as a timeline.
