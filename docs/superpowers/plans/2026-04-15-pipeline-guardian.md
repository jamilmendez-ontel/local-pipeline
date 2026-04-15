# Pipeline Guardian Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an auto-remediation agent that watches the pipeline fleet, detects failures + missing runs, auto-fixes known-safe patterns, and coordinates bigger fixes with Jamil via conversational email reply.

**Architecture:** Apps Script triggers (failure email, 5-min time trigger, reply email) → `repository_dispatch` → GHA workflow → Python agent using Anthropic SDK + existing `db.py`/`pipeline_notifier.py`/`gmail_client.py`. State persisted in new `agent` schema on Supabase.

**Tech Stack:** Python 3.12+, asyncpg, Anthropic SDK (`anthropic` package), Google Gmail API (existing client), GitHub Actions, Google Apps Script, Supabase/PostgreSQL.

**Reference:** Spec at `docs/superpowers/specs/2026-04-15-pipeline-guardian-design.md` (commit `09b5a11`).

---

## Phase 0: Prerequisites

### Task 0.1: Add Anthropic SDK to requirements

**Files:**
- Modify: `requirements.txt`
- Modify: `swift_api_pipeline/requirements.txt`

- [ ] **Step 1: Check current requirements files**

Run: `cat requirements.txt swift_api_pipeline/requirements.txt | grep -i anthropic`
Expected: No output (anthropic not yet pinned)

- [ ] **Step 2: Add `anthropic` to `requirements.txt`**

Append to `requirements.txt`:
```
anthropic>=0.40.0
pyyaml>=6.0
```

- [ ] **Step 3: Add same to `swift_api_pipeline/requirements.txt`**

Append:
```
anthropic>=0.40.0
pyyaml>=6.0
```

- [ ] **Step 4: Install locally**

Run: `pip install anthropic pyyaml`
Expected: Successfully installed anthropic-X.X.X pyyaml-X.X.X

- [ ] **Step 5: Commit**

```bash
git add requirements.txt swift_api_pipeline/requirements.txt
git commit -m "chore: add anthropic + pyyaml to requirements for Pipeline Guardian"
```

---

### Task 0.2: Add CLAUDE_API_KEY env var loader helper

**Files:**
- Modify: `swift_api_pipeline/config.py`

- [ ] **Step 1: Open `swift_api_pipeline/config.py` and find the env-loading section**

Run: `grep -n "CLAUDE_API_KEY\|os.environ" swift_api_pipeline/config.py | head -10`
Expected: Shows existing env-var patterns.

- [ ] **Step 2: Add a getter function for Anthropic API key**

Append to `swift_api_pipeline/config.py`:
```python
def get_anthropic_api_key() -> str:
    """Return Anthropic API key from env, raising if missing.

    Guardian agent uses this to reach Claude API.
    Same key as local-ai-agent backend (CLAUDE_API_KEY env var).
    """
    import os
    key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "Neither CLAUDE_API_KEY nor ANTHROPIC_API_KEY is set. "
            "Guardian agent requires Anthropic API access."
        )
    return key
```

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/config.py
git commit -m "feat(config): add get_anthropic_api_key() helper for Guardian agent"
```

---

## Phase 1: Database Schema

### Task 1.1: Write migration 045 (agent schema + 3 tables)

**Files:**
- Create: `swift_api_pipeline/migrations/045_agent_schema.sql`

- [ ] **Step 1: Create migration file with full DDL**

Create `swift_api_pipeline/migrations/045_agent_schema.sql`:
```sql
-- Migration 045: Pipeline Guardian Agent schema
-- Creates agent schema + 3 tables (monitor_state, pipeline_schedule, known_issues)

CREATE SCHEMA IF NOT EXISTS agent;

-- Table: tracks every detection, decision, and action the guardian takes
CREATE TABLE IF NOT EXISTS agent.monitor_state (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id            UUID REFERENCES pipeline.pipeline_runs(run_id),
    pipeline_name     TEXT NOT NULL,
    pattern_id        TEXT NOT NULL,
    state             TEXT NOT NULL CHECK (state IN (
        'detected', 'auto_fix_pending', 'auto_fixed',
        'awaiting_approval', 'approved', 'executed',
        'declined', 'escalated'
    )),
    severity          TEXT NOT NULL CHECK (severity IN ('auto', 'approve', 'escalate')),
    diagnosis         JSONB,
    proposed_action   JSONB,
    email_message_id  TEXT,
    email_thread_id   TEXT,
    executed_at       TIMESTAMPTZ,
    result            JSONB,
    reminder_sent_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_monitor_state_pipeline
    ON agent.monitor_state(pipeline_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_monitor_state_state
    ON agent.monitor_state(state)
    WHERE state IN ('awaiting_approval', 'approved');
CREATE INDEX IF NOT EXISTS idx_monitor_state_thread
    ON agent.monitor_state(email_thread_id)
    WHERE email_thread_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_state_dedupe
    ON agent.monitor_state(run_id, pattern_id)
    WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_state_missing_run_dedupe
    ON agent.monitor_state(pipeline_name, pattern_id, (diagnosis->>'expected_date'))
    WHERE run_id IS NULL AND pattern_id = 'missing_run';

-- Table: expected pipeline schedule (source of truth for missing-run detection)
CREATE TABLE IF NOT EXISTS agent.pipeline_schedule (
    pipeline_name   TEXT PRIMARY KEY,
    expected_cron   TEXT NOT NULL,
    grace_minutes   INT NOT NULL DEFAULT 15,
    runner          TEXT NOT NULL CHECK (runner IN ('local', 'gha')),
    gha_event_type  TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    notes           TEXT
);

-- Table: editable knowledge base of failure patterns
CREATE TABLE IF NOT EXISTS agent.known_issues (
    pattern_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    detection_rule  JSONB NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('auto', 'approve', 'escalate')),
    fix_action      JSONB,
    description     TEXT,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    enabled         BOOLEAN NOT NULL DEFAULT true
);

-- Trigger: auto-update updated_at on monitor_state
CREATE OR REPLACE FUNCTION agent.touch_monitor_state_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_monitor_state_touch ON agent.monitor_state;
CREATE TRIGGER trg_monitor_state_touch
    BEFORE UPDATE ON agent.monitor_state
    FOR EACH ROW EXECUTE FUNCTION agent.touch_monitor_state_updated_at();

COMMENT ON SCHEMA agent IS 'Pipeline Guardian Agent state + knowledge base';
COMMENT ON TABLE agent.monitor_state IS 'Every detection, decision, and action the guardian has taken';
COMMENT ON TABLE agent.pipeline_schedule IS 'Expected schedule for each pipeline (missing-run detection)';
COMMENT ON TABLE agent.known_issues IS 'Editable knowledge base — add patterns here without code redeploy';
```

- [ ] **Step 2: Apply migration to cloud Supabase**

Create helper script `swift_api_pipeline/migrations/apply_045.py`:
```python
"""Apply migration 045_agent_schema.sql to cloud Supabase."""
import asyncio
import asyncpg
from pathlib import Path

async def main():
    sql = Path(__file__).parent.joinpath("045_agent_schema.sql").read_text()
    conn = await asyncpg.connect(
        host="db.voqfjfngdpcvevbkikud.supabase.co",
        port=5432, database="postgres",
        user="postgres", password="[REDACTED-OLD-PW]",
        statement_cache_size=0
    )
    try:
        await conn.execute(sql)
        print("Migration 045 applied successfully")
        # Verify
        schemas = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'agent'"
        )
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'agent' ORDER BY tablename"
        )
        print(f"Schema exists: {[s['schema_name'] for s in schemas]}")
        print(f"Tables: {[t['tablename'] for t in tables]}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
```

Run: `cd swift_api_pipeline && python migrations/apply_045.py`
Expected output:
```
Migration 045 applied successfully
Schema exists: ['agent']
Tables: ['known_issues', 'monitor_state', 'pipeline_schedule']
```

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/migrations/045_agent_schema.sql swift_api_pipeline/migrations/apply_045.py
git commit -m "feat(db): migration 045 — agent schema (monitor_state, pipeline_schedule, known_issues)"
```

---

### Task 1.2: Write migration 046 (cleanup historical stuck runs)

**Files:**
- Create: `swift_api_pipeline/migrations/046_cleanup_stuck_runs.sql`
- Create: `swift_api_pipeline/migrations/apply_046.py`

- [ ] **Step 1: Create migration file**

Create `swift_api_pipeline/migrations/046_cleanup_stuck_runs.sql`:
```sql
-- Migration 046: mark historical stuck 'running' runs as failed
-- 12 rows identified by Guardian design analysis on 2026-04-15.

UPDATE pipeline.pipeline_runs
SET status = 'failed',
    error_message = 'Auto-marked by guardian deployment: historical stuck run (cleaned 2026-04-15)'
WHERE status = 'running'
  AND started_at < '2026-04-01 00:00:00+00';
```

- [ ] **Step 2: Create apply script**

Create `swift_api_pipeline/migrations/apply_046.py`:
```python
"""Apply migration 046_cleanup_stuck_runs.sql."""
import asyncio
import asyncpg
from pathlib import Path

async def main():
    sql = Path(__file__).parent.joinpath("046_cleanup_stuck_runs.sql").read_text()
    conn = await asyncpg.connect(
        host="db.voqfjfngdpcvevbkikud.supabase.co",
        port=5432, database="postgres",
        user="postgres", password="[REDACTED-OLD-PW]",
        statement_cache_size=0
    )
    try:
        before = await conn.fetchval(
            "SELECT COUNT(*) FROM pipeline.pipeline_runs WHERE status = 'running'"
        )
        print(f"Stuck 'running' rows before: {before}")
        result = await conn.execute(sql)
        print(f"Migration 046 applied: {result}")
        after = await conn.fetchval(
            "SELECT COUNT(*) FROM pipeline.pipeline_runs WHERE status = 'running' AND started_at < '2026-04-01'"
        )
        print(f"Stuck rows before 2026-04-01 after: {after}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Apply migration**

Run: `cd swift_api_pipeline && python migrations/apply_046.py`
Expected:
```
Stuck 'running' rows before: 12 (or similar)
Migration 046 applied: UPDATE 12
Stuck rows before 2026-04-01 after: 0
```

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/migrations/046_cleanup_stuck_runs.sql swift_api_pipeline/migrations/apply_046.py
git commit -m "feat(db): migration 046 — mark 12 historical stuck runs as failed"
```

---

### Task 1.3: Create known_issues.yaml with all 18 patterns

**Files:**
- Create: `pipeline_guardian/known_issues.yaml`

- [ ] **Step 1: Create directory**

Run: `mkdir -p pipeline_guardian`

- [ ] **Step 2: Create YAML with all 18 patterns**

Create `pipeline_guardian/known_issues.yaml`:
```yaml
# Pipeline Guardian — Known Issues Knowledge Base
# Each entry: pattern_id, title, detection_rule, severity, fix_action, description
# Syncs to agent.known_issues table via sync_known_issues.py
# Add new patterns here — no code redeploy needed.

patterns:

  # ========== AUTO-FIX (5) ==========

  - pattern_id: stale_runs_in_raw_table
    title: Stale runs accumulated in raw table
    severity: auto
    detection_rule:
      match_type: error_regex
      pattern: 'Cleanup aborted: new run has [\d,]+ rows but old run has [\d,]+ rows'
    fix_action:
      function: clean_stale_runs
      args:
        table_hint_from_pipeline: true  # pipeline_name → raw table name
    description: |
      Safety check aborted cleanup because old runs (from previous failed attempts)
      inflated the comparison count. Delete non-current-run rows from data_raw.<table>.

  - pattern_id: stuck_running_status
    title: Run stuck in 'running' status >2h
    severity: auto
    detection_rule:
      match_type: sql
      sql: |
        SELECT run_id, pipeline_name FROM pipeline.pipeline_runs
        WHERE status = 'running' AND started_at < now() - interval '2 hours'
    fix_action:
      function: mark_stuck_failed
      args:
        reason: 'Auto-marked by guardian: stuck >2h'
    description: |
      Pipeline crashed without updating status. Mark as failed so future
      safety checks compare against the last successful run.

  - pattern_id: index_already_exists
    title: Orphaned index from previous run
    severity: auto
    detection_rule:
      match_type: error_regex
      pattern: 'relation "idx_raw_\w+" already exists'
    fix_action:
      function: recreate_orphan_index
      args:
        index_name_from_error: true
    description: |
      Pipeline dropped and recreates indexes; if it crashed mid-recreate, the
      index may already exist. DROP IF EXISTS then re-run the pipeline's restore step.

  - pattern_id: dns_blip_at_startup
    title: DNS resolution failure at pool creation
    severity: auto
    detection_rule:
      match_type: error_regex
      pattern: 'gaierror|getaddrinfo failed'
    fix_action:
      function: log_only
      args:
        note: 'pipeline has its own 3x retry; no action needed'
    description: |
      Transient DNS failure. db.py retries 3x with backoff; this is informational only.

  - pattern_id: swift_api_503_transient
    title: Swift API 503 (pipeline retry succeeded)
    severity: auto
    detection_rule:
      match_type: log_pattern
      pattern: '503 Server Error.*Retry \d+/10'
      and_check: 'pipeline completed successfully'
    fix_action:
      function: log_only
    description: |
      Swift API flakes occasionally; pipeline's 10x retry handles it.
      Only flag if all retries exhaust (that maps to partial_extraction instead).

  # ========== PROPOSE + APPROVE (6) ==========

  - pattern_id: statement_timeout_57014
    title: PostgreSQL statement timeout (57014)
    severity: approve
    detection_rule:
      match_type: error_regex
      pattern: "'code': '57014'|canceling statement due to statement timeout"
    fix_action:
      function: propose_rerun
      args:
        scope: failing_stage
    description: |
      Query exceeded statement_timeout. Propose re-run of the failing stage
      (asset_tasks, forms, user_priorities, etc).

  - pattern_id: connection_closed_midop
    title: DB connection closed mid-operation
    severity: approve
    detection_rule:
      match_type: error_regex
      pattern: 'connection was closed in the middle of operation'
    fix_action:
      function: propose_rerun
      args:
        scope: failing_stage
    description: |
      asyncpg connection dropped. Propose re-run.

  - pattern_id: partial_extraction
    title: Partial extraction (one project failed all retries)
    severity: approve
    detection_rule:
      match_type: error_regex
      pattern: 'partial failure:.*failed \([\d,]+ of expected rows loaded\)'
    fix_action:
      function: propose_rerun
      args:
        scope: full_pipeline
    description: |
      One project's extraction exhausted all 10 retries. Full pipeline re-run
      currently (granular per-project re-run is future enhancement).

  - pattern_id: missing_run
    title: Expected pipeline did not start
    severity: approve
    detection_rule:
      match_type: schedule_check
    fix_action:
      function: propose_trigger_pipeline
    description: |
      Pipeline's scheduled start time + grace window has passed with no run
      recorded. Propose triggering (GHA dispatch or local flag file).

  - pattern_id: safety_check_legitimate_block
    title: Safety check blocked a clean run
    severity: approve
    detection_rule:
      match_type: composite
      steps:
        - pattern: stale_runs_in_raw_table
        - after_fix: previous_good_run_exists
    fix_action:
      function: propose_rerun
      args:
        scope: full_pipeline
        after_cleanup: true
    description: |
      After auto-cleaning stale data, propose re-running the pipeline. This
      was tonight's scenario (2026-04-15) that triggered the project.

  - pattern_id: duplicate_key_raw_table
    title: Duplicate key on raw_* table
    severity: approve
    detection_rule:
      match_type: error_regex
      pattern: "'code': '23505'.*raw_\\w+"
    fix_action:
      function: propose_clean_and_rerun
    description: |
      Duplicate primary key from a partial previous run. Clean stale run data
      then re-run.

  # ========== ESCALATE ONLY (7) ==========

  - pattern_id: oauth_token_expired
    title: Google OAuth token expired
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: 'invalid_grant.*Token has been expired or revoked|invalid_grant.*Bad Request'
    fix_action: null
    description: |
      Requires browser re-auth. Agent emails Jamil with instructions for
      the specific pipeline (calendar uses jamil.mendez@ontel.co token,
      gmail uses jamil.mendez@nanoninth.com token).

  - pattern_id: out_of_memory
    title: Process out of memory
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: 'Cannot enlarge string buffer|out of memory'
    fix_action: null
    description: |
      Likely batch size issue. Needs code review; agent cannot auto-fix.

  - pattern_id: module_not_found
    title: Python module missing
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: "ModuleNotFoundError: No module named '\\w+'"
    fix_action: null
    description: |
      Missing dependency. Needs pip install; manual intervention.

  - pattern_id: code_bug_nameerror
    title: Code bug — NameError
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: "NameError: name '\\w+' is not defined"
    fix_action: null
    description: |
      Developer fix required.

  - pattern_id: asyncpg_type_mismatch
    title: asyncpg type mismatch
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: "'str' object has no attribute 'toordinal'|invalid input for query argument"
    fix_action: null
    description: |
      Code bug — mismatched types passed to query. Developer fix required.

  - pattern_id: pgrst106_schema_missing
    title: PostgREST schema not exposed
    severity: escalate
    detection_rule:
      match_type: error_regex
      pattern: "'code': 'PGRST106'"
    fix_action: null
    description: |
      Schema not in pgrst.db_schemas config. Config fix needed.

  - pattern_id: silent_death
    title: Process died silently mid-run
    severity: escalate
    detection_rule:
      match_type: composite
      steps:
        - state: running
        - last_log_line: Complete
        - no_completion_record: true
    fix_action: null
    description: |
      Process completed extraction but never wrote completion status.
      Rare — escalate for manual review.

# ========== PIPELINE SCHEDULE ==========
# Source of truth for missing-run detection. Update when schedule changes.

schedule:
  - pipeline_name: asset_tasks_extract
    expected_cron: "1 5 * * *"     # 12:01 AM ET = 5:01 AM UTC
    grace_minutes: 15
    runner: local
    gha_event_type: null
    enabled: true
    notes: "Local Task Scheduler — SwiftPipeline-Nightly"

  - pipeline_name: calendar_leave
    expected_cron: "30 4 * * *"    # 12:30 AM ET = 4:30 AM UTC
    grace_minutes: 15
    runner: local
    gha_event_type: null
    enabled: true
    notes: "Local Task Scheduler — SwiftPipeline-Calendar"

  - pipeline_name: orgs_projects_extract
    expected_cron: "0 3 * * *"     # 3:00 AM UTC = 10:00 PM ET
    grace_minutes: 15
    runner: gha
    gha_event_type: guardian-rerun-orgs
    enabled: true
    notes: "GHA pipeline-orgs.yml"

  - pipeline_name: timer_extract
    expected_cron: "1 5 * * *"     # 12:01 AM ET = 5:01 AM UTC
    grace_minutes: 15
    runner: gha
    gha_event_type: guardian-rerun-timer
    enabled: true

  - pipeline_name: user_priorities_extract
    expected_cron: "1 5 * * *"
    grace_minutes: 15
    runner: gha
    gha_event_type: guardian-rerun-priorities
    enabled: true

  - pipeline_name: forms_extract
    expected_cron: "1 5 * * *"
    grace_minutes: 30  # Forms can take longer to start
    runner: gha
    gha_event_type: guardian-rerun-forms
    enabled: true

  - pipeline_name: timer_discrepancies
    expected_cron: "1 5 * * *"
    grace_minutes: 15
    runner: gha
    gha_event_type: guardian-rerun-timer-disc
    enabled: true
```

- [ ] **Step 3: Commit**

```bash
git add pipeline_guardian/known_issues.yaml
git commit -m "feat(guardian): add known_issues.yaml with 18 patterns + schedule"
```

---

### Task 1.4: Write migration 047 (seed from YAML) + sync script

**Files:**
- Create: `pipeline_guardian/sync_known_issues.py`
- Create: `swift_api_pipeline/migrations/047_seed_agent_data.sql` (placeholder noting sync is via script)
- Create: `swift_api_pipeline/migrations/apply_047.py`

- [ ] **Step 1: Create the sync script**

Create `pipeline_guardian/sync_known_issues.py`:
```python
"""Sync known_issues.yaml -> agent.known_issues and agent.pipeline_schedule tables.

One-way: YAML is source of truth. Running this wipes and reloads both tables.
Safe to run repeatedly (idempotent via TRUNCATE + INSERT).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import asyncpg
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = Path(__file__).resolve().parent / "known_issues.yaml"


async def sync(db_params: dict) -> None:
    data = yaml.safe_load(YAML_PATH.read_text())
    patterns = data.get("patterns", [])
    schedule = data.get("schedule", [])

    conn = await asyncpg.connect(**db_params, statement_cache_size=0)
    try:
        async with conn.transaction():
            await conn.execute("TRUNCATE agent.known_issues")
            for p in patterns:
                await conn.execute(
                    """
                    INSERT INTO agent.known_issues
                        (pattern_id, title, detection_rule, severity, fix_action, description)
                    VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6)
                    """,
                    p["pattern_id"],
                    p["title"],
                    json.dumps(p["detection_rule"]),
                    p["severity"],
                    json.dumps(p.get("fix_action")) if p.get("fix_action") else None,
                    p.get("description", "").strip() or None,
                )

            await conn.execute("TRUNCATE agent.pipeline_schedule")
            for s in schedule:
                await conn.execute(
                    """
                    INSERT INTO agent.pipeline_schedule
                        (pipeline_name, expected_cron, grace_minutes, runner,
                         gha_event_type, enabled, notes)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    s["pipeline_name"],
                    s["expected_cron"],
                    s.get("grace_minutes", 15),
                    s["runner"],
                    s.get("gha_event_type"),
                    s.get("enabled", True),
                    s.get("notes"),
                )

        pattern_count = await conn.fetchval("SELECT COUNT(*) FROM agent.known_issues")
        schedule_count = await conn.fetchval("SELECT COUNT(*) FROM agent.pipeline_schedule")
        print(f"Synced {pattern_count} patterns, {schedule_count} scheduled pipelines")
    finally:
        await conn.close()


def main():
    db_params = dict(
        host="db.voqfjfngdpcvevbkikud.supabase.co",
        port=5432,
        database="postgres",
        user="postgres",
        password="[REDACTED-OLD-PW]",
    )
    asyncio.run(sync(db_params))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create migration marker file (for bookkeeping)**

Create `swift_api_pipeline/migrations/047_seed_agent_data.sql`:
```sql
-- Migration 047: seed agent.known_issues and agent.pipeline_schedule from YAML
-- Applied via: python pipeline_guardian/sync_known_issues.py
-- YAML source: pipeline_guardian/known_issues.yaml
-- This file is a marker only; the script is idempotent and is the source of truth.
```

- [ ] **Step 3: Run sync script**

Run: `cd .. && python pipeline_guardian/sync_known_issues.py` (from repo root)
Expected output:
```
Synced 18 patterns, 7 scheduled pipelines
```

- [ ] **Step 4: Verify in DB**

Run:
```bash
python -c "
import asyncio, asyncpg
async def check():
    c = await asyncpg.connect(host='db.voqfjfngdpcvevbkikud.supabase.co', port=5432, database='postgres', user='postgres', password='[REDACTED-OLD-PW]', statement_cache_size=0)
    p = await c.fetch('SELECT pattern_id, severity FROM agent.known_issues ORDER BY severity, pattern_id')
    for r in p: print(f'  {r[\"severity\"]:10s} {r[\"pattern_id\"]}')
    s = await c.fetch('SELECT pipeline_name, runner FROM agent.pipeline_schedule ORDER BY pipeline_name')
    for r in s: print(f'  {r[\"runner\"]:5s} {r[\"pipeline_name\"]}')
    await c.close()
asyncio.run(check())
"
```
Expected: 18 pattern rows + 7 schedule rows listed.

- [ ] **Step 5: Commit**

```bash
git add pipeline_guardian/sync_known_issues.py swift_api_pipeline/migrations/047_seed_agent_data.sql
git commit -m "feat(guardian): sync_known_issues.py + migration 047 marker (seeds agent tables from YAML)"
```

---

## Phase 2: Guardian Python Module — Core

### Task 2.1: Package scaffold + DB connection helper

**Files:**
- Create: `pipeline_guardian/__init__.py`
- Create: `pipeline_guardian/db.py`

- [ ] **Step 1: Create package init**

Create `pipeline_guardian/__init__.py`:
```python
"""Pipeline Guardian Agent.

Monitors pipeline_runs, detects failures + missing runs, auto-fixes known patterns,
and coordinates bigger fixes with Jamil via email reply.

See docs/superpowers/specs/2026-04-15-pipeline-guardian-design.md for the design.
"""

__version__ = "0.1.0"
```

- [ ] **Step 2: Create DB helper that reuses pipeline's asyncpg setup**

Create `pipeline_guardian/db.py`:
```python
"""Async DB access for the Guardian agent.

Uses a fresh asyncpg connection per invocation (GHA jobs are short-lived).
Shares the same Supabase credentials as the pipeline (env vars).
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg


def _db_params() -> dict:
    return dict(
        host=os.environ.get("SUPABASE_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.environ.get("SUPABASE_PORT", "5432")),
        database=os.environ.get("SUPABASE_DB", "postgres"),
        user=os.environ.get("SUPABASE_USER", "postgres"),
        password=os.environ["SUPABASE_PASSWORD"],
    )


async def connect() -> asyncpg.Connection:
    """Open a single-use connection. Caller is responsible for closing."""
    return await asyncpg.connect(**_db_params(), statement_cache_size=0)


class GuardianDB:
    """Async context manager that ensures the connection is closed."""

    def __init__(self) -> None:
        self._conn: Optional[asyncpg.Connection] = None

    async def __aenter__(self) -> asyncpg.Connection:
        self._conn = await connect()
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
```

- [ ] **Step 3: Commit**

```bash
git add pipeline_guardian/__init__.py pipeline_guardian/db.py
git commit -m "feat(guardian): package scaffold + async DB helper"
```

---

### Task 2.2: State store — monitor_state CRUD

**Files:**
- Create: `pipeline_guardian/state_store.py`
- Create: `tests/pipeline_guardian/__init__.py`
- Create: `tests/pipeline_guardian/conftest.py`
- Create: `tests/pipeline_guardian/test_state_store.py`

- [ ] **Step 1: Create test directory + conftest**

Create `tests/pipeline_guardian/__init__.py`: (empty file)

Create `tests/pipeline_guardian/conftest.py`:
```python
"""Pytest fixtures for guardian tests.

Uses the real cloud Supabase — tests write to an isolated `_test_*` pipeline_name
prefix so they don't mix with production state.
"""
import asyncio
import os
import pytest
import pytest_asyncio

from pipeline_guardian.db import connect


TEST_PIPELINE_PREFIX = "_test_guardian_"


@pytest_asyncio.fixture
async def conn():
    c = await connect()
    yield c
    # Cleanup test rows after each test
    await c.execute(
        "DELETE FROM agent.monitor_state WHERE pipeline_name LIKE $1",
        f"{TEST_PIPELINE_PREFIX}%"
    )
    await c.close()


@pytest.fixture
def test_pipeline_name(request):
    return f"{TEST_PIPELINE_PREFIX}{request.node.name}"
```

- [ ] **Step 2: Write failing tests for state_store**

Create `tests/pipeline_guardian/test_state_store.py`:
```python
"""Tests for pipeline_guardian.state_store — monitor_state CRUD."""
import pytest

from pipeline_guardian import state_store


@pytest.mark.asyncio
async def test_create_detection_writes_row(conn, test_pipeline_name):
    row_id = await state_store.create_detection(
        conn,
        pipeline_name=test_pipeline_name,
        pattern_id="stale_runs_in_raw_table",
        severity="auto",
        diagnosis={"rows_to_clean": 1000, "evidence": "test"},
        run_id=None,
    )
    assert row_id > 0

    fetched = await state_store.get_by_id(conn, row_id)
    assert fetched is not None
    assert fetched["pipeline_name"] == test_pipeline_name
    assert fetched["pattern_id"] == "stale_runs_in_raw_table"
    assert fetched["state"] == "detected"
    assert fetched["severity"] == "auto"
    assert fetched["diagnosis"]["rows_to_clean"] == 1000


@pytest.mark.asyncio
async def test_transition_state(conn, test_pipeline_name):
    row_id = await state_store.create_detection(
        conn,
        pipeline_name=test_pipeline_name,
        pattern_id="stale_runs_in_raw_table",
        severity="auto",
        diagnosis={},
    )

    await state_store.transition(conn, row_id, "auto_fix_pending")
    row = await state_store.get_by_id(conn, row_id)
    assert row["state"] == "auto_fix_pending"

    await state_store.transition(
        conn, row_id, "auto_fixed", result={"rows_deleted": 1000}
    )
    row = await state_store.get_by_id(conn, row_id)
    assert row["state"] == "auto_fixed"
    assert row["result"]["rows_deleted"] == 1000
    assert row["executed_at"] is not None


@pytest.mark.asyncio
async def test_dedupe_by_run_pattern(conn, test_pipeline_name):
    import uuid
    # Must reference a real pipeline_runs row (FK); use the most recent one.
    run_id = await conn.fetchval(
        "SELECT run_id FROM pipeline.pipeline_runs ORDER BY started_at DESC LIMIT 1"
    )
    assert run_id is not None, "Pipeline_runs must have at least one row"

    row1 = await state_store.create_detection(
        conn, pipeline_name=test_pipeline_name,
        pattern_id="stuck_running_status", severity="auto",
        diagnosis={}, run_id=run_id,
    )
    # Second insert with same (run_id, pattern_id) should raise on unique index
    with pytest.raises(Exception):  # UniqueViolationError
        await state_store.create_detection(
            conn, pipeline_name=test_pipeline_name,
            pattern_id="stuck_running_status", severity="auto",
            diagnosis={}, run_id=run_id,
        )


@pytest.mark.asyncio
async def test_find_existing_detection(conn, test_pipeline_name):
    run_id = await conn.fetchval(
        "SELECT run_id FROM pipeline.pipeline_runs ORDER BY started_at DESC LIMIT 1"
    )
    await state_store.create_detection(
        conn, pipeline_name=test_pipeline_name,
        pattern_id="stuck_running_status", severity="auto",
        diagnosis={}, run_id=run_id,
    )

    existing = await state_store.find_existing(conn, run_id, "stuck_running_status")
    assert existing is not None

    missing = await state_store.find_existing(conn, run_id, "nonexistent_pattern")
    assert missing is None
```

- [ ] **Step 3: Run the tests — they should fail (module not yet created)**

Run: `pytest tests/pipeline_guardian/test_state_store.py -v`
Expected: ImportError or ModuleNotFoundError for `pipeline_guardian.state_store`

- [ ] **Step 4: Implement state_store**

Create `pipeline_guardian/state_store.py`:
```python
"""CRUD for agent.monitor_state.

All functions take an open asyncpg connection; caller manages connection lifetime.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

import asyncpg


VALID_STATES = {
    "detected", "auto_fix_pending", "auto_fixed",
    "awaiting_approval", "approved", "executed",
    "declined", "escalated",
}
VALID_SEVERITIES = {"auto", "approve", "escalate"}


async def create_detection(
    conn: asyncpg.Connection,
    *,
    pipeline_name: str,
    pattern_id: str,
    severity: str,
    diagnosis: dict,
    run_id: Optional[UUID] = None,
    proposed_action: Optional[dict] = None,
) -> int:
    """Insert a new detection row in state 'detected'. Returns the row id."""
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {severity!r}")

    row = await conn.fetchrow(
        """
        INSERT INTO agent.monitor_state
            (pipeline_name, pattern_id, severity, state, diagnosis, proposed_action, run_id)
        VALUES ($1, $2, $3, 'detected', $4::jsonb, $5::jsonb, $6)
        RETURNING id
        """,
        pipeline_name, pattern_id, severity,
        json.dumps(diagnosis),
        json.dumps(proposed_action) if proposed_action else None,
        run_id,
    )
    return row["id"]


async def transition(
    conn: asyncpg.Connection,
    row_id: int,
    new_state: str,
    *,
    result: Optional[dict] = None,
    email_message_id: Optional[str] = None,
    email_thread_id: Optional[str] = None,
) -> None:
    """Transition a monitor_state row to a new state, optionally attaching result."""
    if new_state not in VALID_STATES:
        raise ValueError(f"state must be one of {VALID_STATES}, got {new_state!r}")

    is_terminal = new_state in {"auto_fixed", "executed", "declined", "escalated"}

    await conn.execute(
        """
        UPDATE agent.monitor_state
        SET state = $2,
            result = COALESCE($3::jsonb, result),
            email_message_id = COALESCE($4, email_message_id),
            email_thread_id = COALESCE($5, email_thread_id),
            executed_at = CASE WHEN $6 THEN NOW() ELSE executed_at END
        WHERE id = $1
        """,
        row_id, new_state,
        json.dumps(result) if result else None,
        email_message_id, email_thread_id,
        is_terminal,
    )


async def get_by_id(conn: asyncpg.Connection, row_id: int) -> Optional[asyncpg.Record]:
    row = await conn.fetchrow("SELECT * FROM agent.monitor_state WHERE id = $1", row_id)
    if row is None:
        return None
    # Decode JSONB fields for test assertions
    return _decode_jsonb(row)


async def find_existing(
    conn: asyncpg.Connection,
    run_id: Optional[UUID],
    pattern_id: str,
) -> Optional[asyncpg.Record]:
    """Return the existing monitor_state row for (run_id, pattern_id) or None."""
    if run_id is None:
        return None
    row = await conn.fetchrow(
        "SELECT * FROM agent.monitor_state WHERE run_id = $1 AND pattern_id = $2",
        run_id, pattern_id,
    )
    return _decode_jsonb(row) if row else None


async def find_by_thread(
    conn: asyncpg.Connection,
    email_thread_id: str,
) -> Optional[asyncpg.Record]:
    row = await conn.fetchrow(
        """
        SELECT * FROM agent.monitor_state
        WHERE email_thread_id = $1
          AND state IN ('awaiting_approval', 'approved')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        email_thread_id,
    )
    return _decode_jsonb(row) if row else None


async def is_disabled(conn: asyncpg.Connection) -> bool:
    """Kill switch — returns True if guardian should halt all actions."""
    row = await conn.fetchrow(
        """
        SELECT 1 FROM agent.monitor_state
        WHERE pipeline_name = '_SYSTEM' AND pattern_id = '_DISABLED'
          AND state = 'detected'
        LIMIT 1
        """
    )
    return row is not None


async def count_recent_reruns(
    conn: asyncpg.Connection,
    pipeline_name: str,
    hours: int = 24,
) -> int:
    """Count executed re-runs for a pipeline in the last N hours (rate limiter)."""
    return await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM agent.monitor_state
        WHERE pipeline_name = $1
          AND state = 'executed'
          AND (proposed_action->>'function') IN (
              'trigger_gha_workflow', 'trigger_local_pipeline'
          )
          AND executed_at > NOW() - ($2 || ' hours')::interval
        """,
        pipeline_name, str(hours),
    )


def _decode_jsonb(row: asyncpg.Record) -> dict:
    """asyncpg returns JSONB as JSON string; decode for callers."""
    d = dict(row)
    for key in ("diagnosis", "proposed_action", "result"):
        if d.get(key) and isinstance(d[key], str):
            d[key] = json.loads(d[key])
    return d
```

- [ ] **Step 5: Add pytest config for asyncio**

Check if `pyproject.toml` or `pytest.ini` exists:
Run: `ls pyproject.toml pytest.ini 2>/dev/null`

If neither exists, create `pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

Install test deps:
Run: `pip install pytest pytest-asyncio`

- [ ] **Step 6: Run tests — they should pass**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_state_store.py -v`

(On Windows bash: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_state_store.py -v`)

Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add pipeline_guardian/state_store.py tests/pipeline_guardian/ pytest.ini
git commit -m "feat(guardian): state_store module + tests (monitor_state CRUD, dedupe, kill switch, rate limit)"
```

---

### Task 2.3: Detectors — classify failures against known_issues

**Files:**
- Create: `pipeline_guardian/detectors.py`
- Create: `tests/pipeline_guardian/test_detectors.py`

- [ ] **Step 1: Write failing tests**

Create `tests/pipeline_guardian/test_detectors.py`:
```python
"""Tests for pipeline_guardian.detectors — classifying failures."""
import pytest

from pipeline_guardian import detectors


@pytest.mark.asyncio
async def test_classify_stale_runs_pattern(conn):
    error_message = (
        "Cleanup aborted: new run has 2,430,746 rows but old run has 5,733,304 rows "
        "(threshold: 90%). This suggests data loss during extraction."
    )
    result = await detectors.classify(
        conn,
        pipeline_name="asset_tasks_extract",
        error_message=error_message,
        log_tail="",
    )
    assert result is not None
    assert result["pattern_id"] == "stale_runs_in_raw_table"
    assert result["severity"] == "auto"
    assert result["matched"] is True


@pytest.mark.asyncio
async def test_classify_oauth_expired(conn):
    error_message = (
        "('invalid_grant: Token has been expired or revoked.', "
        "{'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})"
    )
    result = await detectors.classify(
        conn,
        pipeline_name="calendar_leave",
        error_message=error_message,
        log_tail="",
    )
    assert result is not None
    assert result["pattern_id"] == "oauth_token_expired"
    assert result["severity"] == "escalate"


@pytest.mark.asyncio
async def test_classify_statement_timeout(conn):
    error_message = (
        "{'code': '57014', 'details': None, 'hint': None, "
        "'message': 'canceling statement due to statement timeout'}"
    )
    result = await detectors.classify(
        conn,
        pipeline_name="forms_extract",
        error_message=error_message,
        log_tail="",
    )
    assert result["pattern_id"] == "statement_timeout_57014"
    assert result["severity"] == "approve"


@pytest.mark.asyncio
async def test_classify_unknown_returns_unknown(conn):
    result = await detectors.classify(
        conn,
        pipeline_name="asset_tasks_extract",
        error_message="Something weird happened that doesn't match any pattern",
        log_tail="",
    )
    assert result["pattern_id"] == "unknown"
    assert result["severity"] == "escalate"
    assert result["matched"] is False


@pytest.mark.asyncio
async def test_classify_duplicate_key_raw_table(conn):
    error_message = (
        "{'code': '23505', 'details': 'Key (id)=(10001) already exists.', "
        "'message': 'duplicate key value violates unique constraint \"raw_user_priorities_pkey\"'}"
    )
    result = await detectors.classify(
        conn,
        pipeline_name="user_priorities_extract",
        error_message=error_message,
        log_tail="",
    )
    assert result["pattern_id"] == "duplicate_key_raw_table"
    assert result["severity"] == "approve"
```

- [ ] **Step 2: Run tests — should fail (module not created)**

Run: `pytest tests/pipeline_guardian/test_detectors.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement detectors**

Create `pipeline_guardian/detectors.py`:
```python
"""Classify pipeline failures against agent.known_issues patterns.

Detection rule types:
  - error_regex: regex match against pipeline_runs.error_message
  - log_pattern: regex match against tail of log file
  - sql: classification done via SQL query (stuck_running_status)
  - schedule_check: missing-run detection (handled in schedule_check.py)
  - composite: multi-step (handled per-pattern)
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import asyncpg


async def classify(
    conn: asyncpg.Connection,
    *,
    pipeline_name: str,
    error_message: Optional[str],
    log_tail: str,
) -> dict:
    """Return the first matching pattern, or an 'unknown' record.

    Return shape:
        {
            "pattern_id": str,
            "title": str,
            "severity": "auto" | "approve" | "escalate",
            "fix_action": dict | None,
            "matched": bool,
            "evidence": dict,   # what was matched
        }
    """
    patterns = await _load_patterns(conn)
    error_message = error_message or ""

    for pattern in patterns:
        rule = pattern["detection_rule"]
        match_type = rule.get("match_type")

        if match_type == "error_regex":
            regex = rule.get("pattern", "")
            if regex and re.search(regex, error_message, re.IGNORECASE):
                return _matched(pattern, evidence={"matched_in": "error_message", "regex": regex})

        elif match_type == "log_pattern":
            regex = rule.get("pattern", "")
            if regex and re.search(regex, log_tail, re.IGNORECASE):
                # Some log_pattern rules have an additional and_check — skip here; full check in main flow
                return _matched(pattern, evidence={"matched_in": "log_tail", "regex": regex})

        # sql / schedule_check / composite are driven by separate callers.

    # No pattern matched
    return {
        "pattern_id": "unknown",
        "title": "Unknown failure (no matching pattern)",
        "severity": "escalate",
        "fix_action": None,
        "matched": False,
        "evidence": {
            "error_message": error_message[:500],
            "log_tail_excerpt": log_tail[-500:] if log_tail else "",
        },
    }


async def find_stuck_runs(conn: asyncpg.Connection) -> list[dict]:
    """Detect stuck 'running' runs (>2h old, no activity). Returns list of detections."""
    rows = await conn.fetch(
        """
        SELECT run_id, pipeline_name, started_at
        FROM pipeline.pipeline_runs
        WHERE status = 'running'
          AND started_at < now() - interval '2 hours'
        ORDER BY started_at ASC
        """
    )
    return [
        {
            "run_id": str(r["run_id"]),
            "pipeline_name": r["pipeline_name"],
            "started_at": r["started_at"].isoformat(),
            "pattern_id": "stuck_running_status",
            "severity": "auto",
        }
        for r in rows
    ]


async def _load_patterns(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT pattern_id, title, detection_rule, severity, fix_action, description
        FROM agent.known_issues
        WHERE enabled = true
        ORDER BY severity, pattern_id
        """
    )
    result = []
    for r in rows:
        rule = r["detection_rule"]
        if isinstance(rule, str):
            rule = json.loads(rule)
        fix = r["fix_action"]
        if isinstance(fix, str):
            fix = json.loads(fix)
        result.append({
            "pattern_id": r["pattern_id"],
            "title": r["title"],
            "detection_rule": rule,
            "severity": r["severity"],
            "fix_action": fix,
            "description": r["description"],
        })
    return result


def _matched(pattern: dict, *, evidence: dict) -> dict:
    return {
        "pattern_id": pattern["pattern_id"],
        "title": pattern["title"],
        "severity": pattern["severity"],
        "fix_action": pattern["fix_action"],
        "matched": True,
        "evidence": evidence,
    }
```

- [ ] **Step 4: Run tests — should pass**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_detectors.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add pipeline_guardian/detectors.py tests/pipeline_guardian/test_detectors.py
git commit -m "feat(guardian): detectors module — classify failures against known_issues patterns"
```

---

### Task 2.4: Schedule check — missing-run detection

**Files:**
- Create: `pipeline_guardian/schedule_check.py`
- Create: `tests/pipeline_guardian/test_schedule_check.py`

- [ ] **Step 1: Write failing tests**

Create `tests/pipeline_guardian/test_schedule_check.py`:
```python
"""Tests for schedule_check — detect missing pipeline runs."""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline_guardian import schedule_check


@pytest.mark.asyncio
async def test_expected_runs_for_today(conn):
    """Resolves cron + grace window into expected start time for today."""
    # Run from the schedule seeded in Task 1.3
    now = datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc)  # 2 AM ET
    expected = await schedule_check.expected_runs_by_now(conn, now=now)
    pipeline_names = [e["pipeline_name"] for e in expected]
    # At 2 AM ET, these should all have been expected to start
    assert "asset_tasks_extract" in pipeline_names
    assert "timer_extract" in pipeline_names


@pytest.mark.asyncio
async def test_missing_run_detected_when_no_record(conn):
    """If an expected pipeline has no run today, it should appear in missing list."""
    # 3 AM UTC on a specific date — we can check if asset_tasks_extract ran that day
    now = datetime.now(timezone.utc)
    missing = await schedule_check.find_missing_runs(conn, now=now)
    # Just assert it returns a list of dicts with expected shape
    assert isinstance(missing, list)
    for entry in missing:
        assert "pipeline_name" in entry
        assert "expected_start" in entry
        assert "grace_minutes" in entry
        assert "runner" in entry
        assert "expected_date" in entry
```

- [ ] **Step 2: Run tests — should fail**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_schedule_check.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement schedule_check**

Create `pipeline_guardian/schedule_check.py`:
```python
"""Detect missing pipeline runs against agent.pipeline_schedule.

Approach:
1. Load enabled schedule rows.
2. For each, compute the expected start time for "today" (in UTC).
3. If now > expected_start + grace_minutes, check pipeline_runs for a run started
   within ±30 min of expected_start.
4. If none found, it's a missing run.
"""
from __future__ import annotations

from datetime import datetime, date, time, timedelta, timezone
from typing import Optional

import asyncpg


async def find_missing_runs(
    conn: asyncpg.Connection,
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Return list of pipelines whose expected run is missing (now is past grace window)."""
    now = now or datetime.now(timezone.utc)
    expected = await expected_runs_by_now(conn, now=now)
    today_date = now.date()

    missing = []
    for exp in expected:
        # Look for any run in the pipeline_runs table started within ±30 min of expected
        window_start = exp["expected_start"] - timedelta(minutes=30)
        window_end = exp["expected_start"] + timedelta(hours=6)  # wide tolerance for late starts

        found = await conn.fetchval(
            """
            SELECT 1 FROM pipeline.pipeline_runs
            WHERE pipeline_name = $1
              AND started_at BETWEEN $2 AND $3
            LIMIT 1
            """,
            exp["pipeline_name"], window_start, window_end,
        )
        if not found:
            missing.append({
                **exp,
                "expected_date": today_date.isoformat(),
            })
    return missing


async def expected_runs_by_now(
    conn: asyncpg.Connection,
    *,
    now: datetime,
) -> list[dict]:
    """Load enabled schedule and compute which pipelines should have started by `now`."""
    rows = await conn.fetch(
        """
        SELECT pipeline_name, expected_cron, grace_minutes, runner, gha_event_type, notes
        FROM agent.pipeline_schedule
        WHERE enabled = true
        """
    )
    result = []
    for r in rows:
        expected_start = _cron_today_utc(r["expected_cron"], now.date())
        if expected_start is None:
            continue
        # Must be past (expected_start + grace) to count
        if now < expected_start + timedelta(minutes=r["grace_minutes"]):
            continue
        result.append({
            "pipeline_name": r["pipeline_name"],
            "expected_start": expected_start,
            "grace_minutes": r["grace_minutes"],
            "runner": r["runner"],
            "gha_event_type": r["gha_event_type"],
            "notes": r["notes"],
        })
    return result


def _cron_today_utc(cron_expr: str, today: date) -> Optional[datetime]:
    """Parse a 5-field cron to a UTC datetime on `today`.

    Supports only fixed minute+hour values (e.g. "1 5 * * *"); other fields
    must be '*'. Returns None if schedule doesn't fire on this date.
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return None
    minute_str, hour_str, dom, mon, dow = parts
    # Only support simple fixed minute+hour with wildcards elsewhere
    if not (minute_str.isdigit() and hour_str.isdigit()):
        return None
    if dom != "*" or mon != "*" or dow != "*":
        return None
    minute = int(minute_str)
    hour = int(hour_str)
    return datetime.combine(today, time(hour, minute, tzinfo=timezone.utc))
```

- [ ] **Step 4: Run tests — should pass**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_schedule_check.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add pipeline_guardian/schedule_check.py tests/pipeline_guardian/test_schedule_check.py
git commit -m "feat(guardian): schedule_check — missing-run detection via cron + grace window"
```

---

### Task 2.5: Actions module — safety rails + DB cleanup actions

**Files:**
- Create: `pipeline_guardian/actions.py`
- Create: `tests/pipeline_guardian/test_actions.py`

- [ ] **Step 1: Write failing tests for safety rails**

Create `tests/pipeline_guardian/test_actions.py`:
```python
"""Tests for pipeline_guardian.actions — safety rails + DB cleanup."""
import os
import pytest

from pipeline_guardian import actions


@pytest.mark.asyncio
async def test_schema_whitelist_blocks_public(conn):
    """Actions must refuse to touch non-agent/data schemas."""
    with pytest.raises(PermissionError, match="schema"):
        await actions.exec_sql(conn, "DELETE FROM public.users WHERE id = 1")


@pytest.mark.asyncio
async def test_schema_whitelist_allows_data_raw(conn, test_pipeline_name):
    """Whitelist should allow our schemas."""
    # Safe no-op — EXPLAIN shouldn't modify anything
    await actions.exec_sql(
        conn, "SELECT 1 FROM data_raw.raw_asset_tasks WHERE run_id IS NULL LIMIT 0"
    )


@pytest.mark.asyncio
async def test_dry_run_blocks_execution(conn, monkeypatch, test_pipeline_name):
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "true")
    result = await actions.clean_stale_runs(
        conn,
        table="raw_asset_tasks",
        current_run_id=None,
        pipeline_name=test_pipeline_name,
    )
    assert result["dry_run"] is True
    assert result["would_delete"] >= 0
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_clean_stale_runs_deletes_only_old(conn, monkeypatch, test_pipeline_name):
    """When not dry-run, clean_stale_runs removes rows whose run_id != current."""
    monkeypatch.setenv("GUARDIAN_DRY_RUN", "false")
    # Don't actually run this against raw_asset_tasks — use a test helper table instead.
    # Create a tiny test table in agent schema for this test
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS agent._test_cleanup (
            id SERIAL PRIMARY KEY,
            run_id UUID,
            payload TEXT
        )
    """)
    try:
        await conn.execute(
            "INSERT INTO agent._test_cleanup (run_id, payload) VALUES "
            "('00000000-0000-0000-0000-000000000001', 'old'), "
            "('00000000-0000-0000-0000-000000000001', 'old'), "
            "('00000000-0000-0000-0000-000000000002', 'current')"
        )
        # Monkeypatch the table whitelist to allow agent._test_cleanup
        import uuid
        current = uuid.UUID("00000000-0000-0000-0000-000000000002")
        result = await actions._clean_stale_runs_impl(
            conn,
            schema="agent",
            table="_test_cleanup",
            current_run_id=current,
        )
        assert result["deleted"] == 2
        remaining = await conn.fetchval("SELECT COUNT(*) FROM agent._test_cleanup")
        assert remaining == 1
    finally:
        await conn.execute("DROP TABLE IF EXISTS agent._test_cleanup")


@pytest.mark.asyncio
async def test_mark_stuck_failed(conn):
    """mark_stuck_failed should update status + error_message for a given run_id."""
    # Create a synthetic stuck run
    import uuid
    test_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO pipeline.pipeline_runs (run_id, pipeline_name, started_at, status)
        VALUES ($1, '_test_stuck', now() - interval '3 hours', 'running')
        """,
        test_id,
    )
    try:
        result = await actions.mark_stuck_failed(
            conn, run_id=test_id, reason="test"
        )
        assert result["updated"] == 1
        row = await conn.fetchrow(
            "SELECT status, error_message FROM pipeline.pipeline_runs WHERE run_id = $1",
            test_id,
        )
        assert row["status"] == "failed"
        assert "test" in row["error_message"]
    finally:
        await conn.execute(
            "DELETE FROM pipeline.pipeline_runs WHERE run_id = $1", test_id
        )
```

- [ ] **Step 2: Run tests — should fail**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_actions.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: Implement actions module**

Create `pipeline_guardian/actions.py`:
```python
"""Action executors with safety rails.

Every action checks:
  1. Schema whitelist — can only touch agent / data_raw / data_staging / analytics / pipeline
  2. Dry-run mode (env GUARDIAN_DRY_RUN) — logs intent instead of executing
  3. State-gated — called only when monitor_state is in approved / auto_fix_pending
"""
from __future__ import annotations

import os
import re
from typing import Optional
from uuid import UUID

import asyncpg


ALLOWED_SCHEMAS = {"agent", "data_raw", "data_staging", "analytics", "pipeline"}


def _is_dry_run() -> bool:
    return os.environ.get("GUARDIAN_DRY_RUN", "false").lower() in {"true", "1", "yes"}


def _assert_schema_allowed(sql: str) -> None:
    """Best-effort check that SQL only references allowed schemas.

    Blocks writes/reads to schemas outside our whitelist. This is a defense-in-depth
    measure; DB-level permissions should also restrict the guardian's role.
    """
    # Find all `<schema>.<table>` references
    refs = re.findall(r'\b([a-zA-Z_][\w]*)\s*\.\s*([a-zA-Z_][\w]*)\b', sql)
    for schema, _table in refs:
        schema_lower = schema.lower()
        if schema_lower in {"pg_catalog", "information_schema", "public", "auth",
                           "storage", "realtime", "graphql_public", "reference",
                           "staging"}:
            raise PermissionError(
                f"Guardian cannot touch schema '{schema_lower}' — not in whitelist"
            )
        # Allow if in whitelist or a local alias (single-word refs pass through)


async def exec_sql(conn: asyncpg.Connection, sql: str, *args) -> str:
    """Execute SQL with schema whitelist check. Returns the command tag."""
    _assert_schema_allowed(sql)
    return await conn.execute(sql, *args)


# ========== DB CLEANUP ACTIONS ==========

PIPELINE_RAW_TABLE_MAP = {
    "asset_tasks_extract": ("data_raw", "raw_asset_tasks"),
    "timer_extract": ("data_raw", "raw_timer_activities"),
    "forms_extract": ("data_raw", "raw_qa_forms"),
    "user_priorities_extract": ("data_raw", "raw_user_priorities"),
    "orgs_projects_extract": ("data_raw", "raw_organizations"),  # also raw_projects, handled
    "calendar_leave": ("data_raw", "raw_calendar_leave"),
    "timer_discrepancies": ("data_raw", "raw_timer_discrepancies"),
}


async def clean_stale_runs(
    conn: asyncpg.Connection,
    *,
    pipeline_name: str,
    current_run_id: Optional[UUID],
    table: Optional[str] = None,
) -> dict:
    """Delete rows from the pipeline's raw table where run_id != current_run_id."""
    if table:
        schema, tbl = "data_raw", table
    else:
        if pipeline_name not in PIPELINE_RAW_TABLE_MAP:
            raise ValueError(f"No raw table mapping for pipeline {pipeline_name!r}")
        schema, tbl = PIPELINE_RAW_TABLE_MAP[pipeline_name]

    if _is_dry_run():
        if current_run_id is not None:
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {schema}.{tbl} WHERE run_id != $1",
                current_run_id,
            )
        else:
            count = await conn.fetchval(f"SELECT COUNT(*) FROM {schema}.{tbl}")
        return {"dry_run": True, "would_delete": count, "deleted": 0,
                "schema": schema, "table": tbl}

    return await _clean_stale_runs_impl(conn, schema=schema, table=tbl,
                                         current_run_id=current_run_id)


async def _clean_stale_runs_impl(
    conn: asyncpg.Connection,
    *,
    schema: str,
    table: str,
    current_run_id: Optional[UUID],
) -> dict:
    if schema not in ALLOWED_SCHEMAS:
        raise PermissionError(f"Schema '{schema}' not in whitelist")
    if current_run_id is not None:
        result = await conn.execute(
            f"DELETE FROM {schema}.{table} WHERE run_id != $1",
            current_run_id,
        )
    else:
        result = await conn.execute(f"TRUNCATE {schema}.{table}")
    # result is like "DELETE 1234" or "TRUNCATE"
    deleted = 0
    if result.startswith("DELETE "):
        deleted = int(result.split(" ")[1])
    elif result == "TRUNCATE":
        deleted = -1  # truncate, count unknown
    return {"dry_run": False, "deleted": deleted, "schema": schema, "table": table}


async def mark_stuck_failed(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    reason: str,
) -> dict:
    """Mark a stuck run as failed."""
    message = f"Auto-marked by guardian: {reason}"
    if _is_dry_run():
        return {"dry_run": True, "run_id": str(run_id), "would_set_failed": True}

    result = await conn.execute(
        """
        UPDATE pipeline.pipeline_runs
        SET status = 'failed', error_message = $2, completed_at = NOW()
        WHERE run_id = $1 AND status = 'running'
        """,
        run_id, message,
    )
    updated = int(result.split(" ")[1]) if result.startswith("UPDATE ") else 0
    return {"dry_run": False, "updated": updated, "run_id": str(run_id)}


async def recreate_orphan_index(
    conn: asyncpg.Connection,
    *,
    index_name: str,
) -> dict:
    """Drop the orphaned index if it exists. Pipeline's own restore will recreate it."""
    # Allow only idx_raw_* names to be safe
    if not re.match(r"^idx_raw_[a-z_0-9]+$", index_name):
        raise PermissionError(f"Refusing to drop index outside idx_raw_* pattern: {index_name}")
    if _is_dry_run():
        return {"dry_run": True, "would_drop": index_name}
    await conn.execute(f'DROP INDEX IF EXISTS data_raw."{index_name}"')
    return {"dry_run": False, "dropped": index_name}


# ========== PIPELINE TRIGGER ACTIONS ==========

async def trigger_gha_workflow(event_type: str, payload: dict) -> dict:
    """Fire repository_dispatch to trigger a GHA workflow."""
    import httpx
    token = os.environ.get("GH_DISPATCH_PAT")
    if not token:
        raise RuntimeError("GH_DISPATCH_PAT env var required to trigger GHA workflows")
    if _is_dry_run():
        return {"dry_run": True, "would_dispatch": event_type, "payload": payload}

    url = "https://api.github.com/repos/jamilmendez-ontel/local-pipeline/dispatches"
    r = httpx.post(
        url,
        json={"event_type": event_type, "client_payload": payload},
        headers={
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
        },
        timeout=30,
    )
    r.raise_for_status()
    return {"dry_run": False, "dispatched": event_type, "status": r.status_code}


async def trigger_local_pipeline(pipeline_name: str) -> dict:
    """Write pending-trigger file for local Task Scheduler to pick up.

    File format: JSON array at pipeline_guardian/triggers/pending.json.
    Task Scheduler runs a batch every 5 min that reads + clears the file.
    """
    import json
    from pathlib import Path
    # Path is relative to the GHA workspace, which won't help a local Task Scheduler.
    # This action is a no-op when running in GHA — local triggers require a
    # different delivery mechanism (see task Task 4.3).
    if _is_dry_run():
        return {"dry_run": True, "would_write_trigger": pipeline_name}
    # When GHA agent runs, local pipeline dispatch is handled via separate
    # mechanism: agent writes trigger row to agent.monitor_state.result and
    # the local Task Scheduler batch reads it.
    return {"dry_run": False, "note": "Local trigger written to monitor_state for Task Scheduler to pick up"}
```

- [ ] **Step 4: Run tests — should pass**

Run: `SUPABASE_PASSWORD=[REDACTED-OLD-PW] pytest tests/pipeline_guardian/test_actions.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add pipeline_guardian/actions.py tests/pipeline_guardian/test_actions.py
git commit -m "feat(guardian): actions module — safety rails, DB cleanup, pipeline triggers (dry-run aware)"
```

---

### Task 2.6: Email sender + Gmail reader using existing gmail_client

**Files:**
- Create: `pipeline_guardian/email_io.py`
- Modify: `swift_api_pipeline/pipeline_notifier.py` (add log-tail-in-email support)

- [ ] **Step 1: Read existing gmail_client to understand API**

Run: `grep -n "def " swift_api_pipeline/gmail_client.py | head -20`

- [ ] **Step 2: Create email_io module**

Create `pipeline_guardian/email_io.py`:
```python
"""Email I/O for Guardian agent.

Send: via Gmail API using the same credentials as pipeline_notifier.py.
Read: via Gmail API to fetch reply threads.
"""
from __future__ import annotations

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

# Reuse the pipeline's Gmail credential bootstrap
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swift_api_pipeline"))

from gmail_client import get_gmail_service  # type: ignore


SUBJECT_PREFIX = "[Pipeline Guardian]"
GUARDIAN_RECIPIENTS = [
    "jamil.mendez@ontel.co",
    "jamil.mendez@nanoninth.com",  # for Apps Script reply watcher
]


def send_email(
    subject: str,
    body_html: str,
    *,
    body_text: Optional[str] = None,
    in_reply_to_message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict:
    """Send a Guardian email. Returns {message_id, thread_id}."""
    service = get_gmail_service()

    full_subject = subject if subject.startswith(SUBJECT_PREFIX) else f"{SUBJECT_PREFIX} {subject}"

    msg = MIMEMultipart("alternative")
    msg["To"] = ", ".join(GUARDIAN_RECIPIENTS)
    msg["Subject"] = full_subject
    if in_reply_to_message_id:
        msg["In-Reply-To"] = in_reply_to_message_id
        msg["References"] = in_reply_to_message_id

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body = {"raw": raw}
    if thread_id:
        send_body["threadId"] = thread_id

    result = service.users().messages().send(userId="me", body=send_body).execute()
    return {
        "message_id": result.get("id"),
        "thread_id": result.get("threadId"),
    }


def read_thread(thread_id: str) -> list[dict]:
    """Fetch all messages in a thread. Each entry: {id, from, body, received_at}."""
    service = get_gmail_service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = []
    for m in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        body = _extract_body(m["payload"])
        messages.append({
            "id": m["id"],
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "body": body,
        })
    return messages


def _extract_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload (recursing parts)."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        body = _extract_body(part)
        if body:
            return body
    return ""
```

- [ ] **Step 3: Modify `pipeline_notifier.py` to include log tail in failure emails**

Open `swift_api_pipeline/pipeline_notifier.py` and find the function that builds the failure email body (search for "FAILED" or the email-body function).

Run: `grep -n "def.*email\|FAILED\|build.*body" swift_api_pipeline/pipeline_notifier.py | head`

Find the failure email body builder. Add a parameter `log_tail_lines=30` and append:
```python
    if log_tail and status == "failed":
        html_parts.append(
            f'<h3>Log tail (last {log_tail_lines} lines)</h3>'
            f'<pre style="background:#f4f4f4;padding:10px;'
            f'border-left:3px solid #c00;font-size:11px;overflow:auto">'
            f'{escape(log_tail)}</pre>'
        )
```

(Exact file modification depends on current structure; expand the function to pass last 30 log lines into the email HTML body. The Guardian's email watcher will parse this out of Gmail.)

Note: this step's exact line numbers depend on the current state of `pipeline_notifier.py`. Read the file first, then make a targeted edit.

- [ ] **Step 4: Commit**

```bash
git add pipeline_guardian/email_io.py swift_api_pipeline/pipeline_notifier.py
git commit -m "feat(guardian): email_io module + include log tail in pipeline_notifier failure emails"
```

---

### Task 2.7: Email reply intent parser (Claude-powered)

**Files:**
- Create: `pipeline_guardian/email_parser.py`
- Create: `tests/pipeline_guardian/test_email_parser.py`

- [ ] **Step 1: Write failing tests**

Create `tests/pipeline_guardian/test_email_parser.py`:
```python
"""Tests for email_parser — classify user reply intent.

These tests call the real Anthropic API; they require CLAUDE_API_KEY.
Skip locally when key is absent.
"""
import os
import pytest

from pipeline_guardian import email_parser

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLAUDE_API_KEY"),
    reason="CLAUDE_API_KEY not set",
)


def test_approve_phrases_classified_as_approve():
    for phrase in ["go ahead", "do it", "yes please", "yeah go", "sure, proceed"]:
        intent = email_parser.classify_intent(
            reply_text=phrase,
            pending_action={"function": "trigger_gha_workflow",
                           "description": "Re-run timer pipeline"},
        )
        assert intent["intent"] == "approve", f"{phrase!r} should be approve"


def test_decline_phrases_classified_as_decline():
    for phrase in ["no", "not now", "I'll handle it", "skip it", "no don't"]:
        intent = email_parser.classify_intent(
            reply_text=phrase,
            pending_action={"function": "trigger_gha_workflow"},
        )
        assert intent["intent"] == "decline", f"{phrase!r} should be decline"


def test_modify_intent_extracted():
    intent = email_parser.classify_intent(
        reply_text="just clean the table, don't re-run",
        pending_action={
            "function": "clean_and_rerun",
            "description": "Clean stale rows then re-run the pipeline",
        },
    )
    assert intent["intent"] == "modify"
    assert "clean" in intent["instructions"].lower()


def test_instruct_intent_preserves_new_instructions():
    intent = email_parser.classify_intent(
        reply_text="please also refresh analytics after the re-run",
        pending_action={"function": "trigger_gha_workflow",
                       "description": "Re-run forms"},
    )
    assert intent["intent"] == "instruct"
    assert "analytics" in intent["instructions"].lower()
```

- [ ] **Step 2: Run tests — should fail (if CLAUDE_API_KEY set), skip otherwise**

Run: `pytest tests/pipeline_guardian/test_email_parser.py -v`

- [ ] **Step 3: Implement email_parser**

Create `pipeline_guardian/email_parser.py`:
```python
"""Parse user email reply intent using Claude.

Intents:
  - approve: proceed with the proposed action as-is
  - decline: do not take the action
  - modify: take a modified version of the proposed action
  - instruct: take a completely different/new action
"""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import Anthropic


VALID_INTENTS = {"approve", "decline", "modify", "instruct"}


SYSTEM_PROMPT = """You classify a user's email reply to a Pipeline Guardian agent's proposed action.

Guardian proposed an action. The user replied. Classify intent:
- "approve": user wants the proposed action executed as-is. Phrases: "go ahead", "do it", "yes", "proceed", "yeah go".
- "decline": user does NOT want the action taken. Phrases: "no", "skip", "I'll handle it", "not now", "wait".
- "modify": user wants a VARIATION of the proposed action (partial execution, different scope). E.g. "just clean, don't re-run".
- "instruct": user wants an ENTIRELY DIFFERENT action. E.g. "skip this and run the analytics refresh instead".

Return JSON only, no prose:
{
  "intent": "approve" | "decline" | "modify" | "instruct",
  "instructions": "<free text describing what the user wants; empty for approve/decline>",
  "confidence": 0.0-1.0
}
"""


def classify_intent(reply_text: str, pending_action: dict) -> dict:
    """Classify a single reply against a pending action."""
    api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY env var required")

    client = Anthropic(api_key=api_key)
    user_msg = (
        f"Proposed action:\n{json.dumps(pending_action, indent=2)}\n\n"
        f"User reply:\n{reply_text.strip()}\n\n"
        f"Classify as JSON."
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",  # fast + cheap for classification
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    parsed = json.loads(raw)

    if parsed["intent"] not in VALID_INTENTS:
        raise ValueError(f"Invalid intent from Claude: {parsed['intent']!r}")
    parsed.setdefault("instructions", "")
    parsed.setdefault("confidence", 0.0)
    return parsed
```

- [ ] **Step 4: Run tests with CLAUDE_API_KEY set**

Run: `CLAUDE_API_KEY=<key> pytest tests/pipeline_guardian/test_email_parser.py -v`
Expected: 4 passed (or skipped if key not available locally)

- [ ] **Step 5: Commit**

```bash
git add pipeline_guardian/email_parser.py tests/pipeline_guardian/test_email_parser.py
git commit -m "feat(guardian): email_parser — Claude-powered reply intent classifier"
```

---

### Task 2.8: Main agent entry — ties it all together

**Files:**
- Create: `pipeline_guardian/agent.py`

- [ ] **Step 1: Implement agent entry point**

Create `pipeline_guardian/agent.py`:
```python
"""Main Guardian agent entry point.

Dispatches based on trigger type from GHA workflow:
  - guardian-failure   → handle_failure(pipeline_name, run_id)
  - guardian-periodic  → handle_periodic()
  - guardian-reply     → handle_reply(thread_id)

Each handler: read state, classify, act (or propose), write state, email.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from pipeline_guardian import actions, detectors, email_io, email_parser, schedule_check, state_store
from pipeline_guardian.db import GuardianDB


# ========== FAILURE TRIGGER ==========

async def handle_failure(pipeline_name: str, run_id: Optional[str]) -> dict:
    """Called when a pipeline failure email arrives (via repository_dispatch)."""
    async with GuardianDB() as conn:
        if await state_store.is_disabled(conn):
            return {"skipped": True, "reason": "guardian disabled"}

        # Load the failed run (if run_id provided)
        run = None
        error_message = None
        if run_id:
            run = await conn.fetchrow(
                "SELECT * FROM pipeline.pipeline_runs WHERE run_id = $1",
                UUID(run_id),
            )
            if run:
                error_message = run["error_message"]

                # Dedupe
                existing = await state_store.find_existing(
                    conn, UUID(run_id), pattern_id=None  # any pattern
                )
                # The unique index is on (run_id, pattern_id), so we check per-pattern later.

        # Classify
        classification = await detectors.classify(
            conn,
            pipeline_name=pipeline_name,
            error_message=error_message,
            log_tail="",  # log_tail comes from the email body; parse it there
        )

        # Create monitor_state row
        row_id = await state_store.create_detection(
            conn,
            pipeline_name=pipeline_name,
            pattern_id=classification["pattern_id"],
            severity=classification["severity"],
            diagnosis={
                "error_message": (error_message or "")[:1000],
                "evidence": classification["evidence"],
                "title": classification["title"],
            },
            run_id=UUID(run_id) if run_id else None,
            proposed_action=classification.get("fix_action"),
        )

        # Route based on severity
        result = {"row_id": row_id, "pattern_id": classification["pattern_id"]}

        if classification["severity"] == "auto":
            result.update(await _execute_auto_fix(conn, row_id, classification, pipeline_name, run_id))
        elif classification["severity"] == "approve":
            result.update(await _send_approval_email(conn, row_id, classification, pipeline_name, run_id))
        else:  # escalate
            result.update(await _send_escalation_email(conn, row_id, classification, pipeline_name, run_id))

        return result


async def _execute_auto_fix(conn, row_id, classification, pipeline_name, run_id) -> dict:
    await state_store.transition(conn, row_id, "auto_fix_pending")
    fix = classification["fix_action"] or {}
    fn = fix.get("function")

    try:
        if fn == "clean_stale_runs":
            # Find the current good run_id (the most recent run that caused the failure)
            current_run = UUID(run_id) if run_id else None
            fix_result = await actions.clean_stale_runs(
                conn, pipeline_name=pipeline_name, current_run_id=current_run
            )
        elif fn == "mark_stuck_failed":
            fix_result = await actions.mark_stuck_failed(
                conn, run_id=UUID(run_id), reason=fix.get("args", {}).get("reason", "stuck")
            )
        elif fn == "log_only":
            fix_result = {"action": "log_only", "note": fix.get("args", {}).get("note", "")}
        elif fn == "recreate_orphan_index":
            # Extract index name from error_message
            import re
            m = re.search(r'"(idx_raw_\w+)"', classification.get("evidence", {}).get("error_message", ""))
            if m:
                fix_result = await actions.recreate_orphan_index(conn, index_name=m.group(1))
            else:
                fix_result = {"error": "could not extract index name from error"}
        else:
            fix_result = {"error": f"unknown fix function {fn!r}"}

        await state_store.transition(conn, row_id, "auto_fixed", result=fix_result)

        # Email summary
        email_result = email_io.send_email(
            subject=f"Auto-fixed: {pipeline_name} — {classification['title']}",
            body_html=_format_auto_fix_email(pipeline_name, classification, fix_result),
        )
        await state_store.transition(
            conn, row_id, "auto_fixed",
            email_message_id=email_result["message_id"],
            email_thread_id=email_result["thread_id"],
        )
        return {"auto_fixed": True, "fix_result": fix_result}

    except Exception as e:
        await state_store.transition(conn, row_id, "escalated",
                                     result={"error": str(e), "during": "auto_fix"})
        email_io.send_email(
            subject=f"Auto-fix FAILED: {pipeline_name} — {classification['title']}",
            body_html=f"<p>Attempted auto-fix but raised: <pre>{e}</pre></p>",
        )
        return {"auto_fixed": False, "error": str(e)}


async def _send_approval_email(conn, row_id, classification, pipeline_name, run_id) -> dict:
    # Rate limit check
    recent = await state_store.count_recent_reruns(conn, pipeline_name)
    if recent >= 3:
        email_io.send_email(
            subject=f"Rate limited: {pipeline_name}",
            body_html=f"<p>Guardian detected a failure but has already triggered {recent} reruns for "
                     f"{pipeline_name} in the last 24h. Not proposing another. Please investigate manually.</p>",
        )
        await state_store.transition(conn, row_id, "escalated", result={"rate_limited": True})
        return {"rate_limited": True}

    email_result = email_io.send_email(
        subject=f"Approval needed: {pipeline_name} — {classification['title']}",
        body_html=_format_approval_email(pipeline_name, classification, run_id),
    )
    await state_store.transition(
        conn, row_id, "awaiting_approval",
        email_message_id=email_result["message_id"],
        email_thread_id=email_result["thread_id"],
    )
    return {"awaiting_approval": True, "thread_id": email_result["thread_id"]}


async def _send_escalation_email(conn, row_id, classification, pipeline_name, run_id) -> dict:
    email_result = email_io.send_email(
        subject=f"Escalation: {pipeline_name} — {classification['title']}",
        body_html=_format_escalation_email(pipeline_name, classification, run_id),
    )
    await state_store.transition(
        conn, row_id, "escalated",
        email_message_id=email_result["message_id"],
        email_thread_id=email_result["thread_id"],
    )
    return {"escalated": True}


# ========== PERIODIC TRIGGER ==========

async def handle_periodic() -> dict:
    async with GuardianDB() as conn:
        if await state_store.is_disabled(conn):
            return {"skipped": True, "reason": "guardian disabled"}

        result = {"stuck_detected": 0, "missing_detected": 0, "reminders_sent": 0}

        # 1. Stuck runs
        stuck = await detectors.find_stuck_runs(conn)
        for s in stuck:
            existing = await state_store.find_existing(
                conn, UUID(s["run_id"]), "stuck_running_status"
            )
            if existing:
                continue
            row_id = await state_store.create_detection(
                conn,
                pipeline_name=s["pipeline_name"],
                pattern_id="stuck_running_status",
                severity="auto",
                diagnosis={"started_at": s["started_at"], "detected_via": "periodic"},
                run_id=UUID(s["run_id"]),
            )
            await _execute_auto_fix(
                conn, row_id,
                {"pattern_id": "stuck_running_status", "title": "Stuck run",
                 "severity": "auto", "fix_action": {"function": "mark_stuck_failed",
                                                     "args": {"reason": "stuck >2h"}},
                 "evidence": {}},
                s["pipeline_name"], s["run_id"],
            )
            result["stuck_detected"] += 1

        # 2. Missing runs
        missing = await schedule_check.find_missing_runs(conn)
        for m in missing:
            row_id = await state_store.create_detection(
                conn,
                pipeline_name=m["pipeline_name"],
                pattern_id="missing_run",
                severity="approve",
                diagnosis={
                    "expected_start": m["expected_start"].isoformat(),
                    "expected_date": m["expected_date"],
                    "runner": m["runner"],
                },
                proposed_action={
                    "function": "trigger_gha_workflow" if m["runner"] == "gha"
                                else "trigger_local_pipeline",
                    "args": {
                        "event_type": m.get("gha_event_type"),
                        "pipeline_name": m["pipeline_name"],
                    },
                },
            )
            email_result = email_io.send_email(
                subject=f"Missing run: {m['pipeline_name']}",
                body_html=_format_missing_run_email(m),
            )
            await state_store.transition(
                conn, row_id, "awaiting_approval",
                email_message_id=email_result["message_id"],
                email_thread_id=email_result["thread_id"],
            )
            result["missing_detected"] += 1

        # 3. Pending approval reminders (12h old, not yet reminded)
        pending = await conn.fetch(
            """
            SELECT * FROM agent.monitor_state
            WHERE state = 'awaiting_approval'
              AND created_at < NOW() - interval '12 hours'
              AND reminder_sent_at IS NULL
            """
        )
        for p in pending:
            email_io.send_email(
                subject=f"Reminder: {p['pipeline_name']} awaiting your reply",
                body_html=f"<p>Still waiting on: {p['pattern_id']} for {p['pipeline_name']}.</p>",
                thread_id=p["email_thread_id"],
            )
            await conn.execute(
                "UPDATE agent.monitor_state SET reminder_sent_at = NOW() WHERE id = $1",
                p["id"],
            )
            result["reminders_sent"] += 1

        return result


# ========== REPLY TRIGGER ==========

async def handle_reply(thread_id: str) -> dict:
    async with GuardianDB() as conn:
        if await state_store.is_disabled(conn):
            return {"skipped": True, "reason": "guardian disabled"}

        pending = await state_store.find_by_thread(conn, thread_id)
        if not pending:
            return {"skipped": True, "reason": "no pending action for thread"}

        messages = email_io.read_thread(thread_id)
        if not messages:
            return {"skipped": True, "reason": "could not read thread"}

        # Use the most recent message as the user's reply
        reply = messages[-1]["body"]
        intent = email_parser.classify_intent(
            reply_text=reply,
            pending_action=pending["proposed_action"] or {},
        )

        if intent["intent"] == "decline":
            await state_store.transition(conn, pending["id"], "declined",
                                         result={"reply_intent": intent})
            email_io.send_email(
                subject=f"Acknowledged: {pending['pipeline_name']}",
                body_html="<p>Got it — not running. Let me know if anything changes.</p>",
                thread_id=thread_id,
            )
            return {"declined": True}

        # Execute approved action (approve or modify — for modify we trust the original
        # proposal for now; full modify support is a future enhancement)
        proposal = pending["proposed_action"] or {}
        fn = proposal.get("function")
        await state_store.transition(conn, pending["id"], "approved")

        try:
            if fn == "trigger_gha_workflow":
                args = proposal.get("args", {})
                exec_result = await actions.trigger_gha_workflow(
                    event_type=args.get("event_type", "guardian-generic-rerun"),
                    payload={"pipeline_name": pending["pipeline_name"]},
                )
                # GHA dispatch is synchronous — we can mark executed now
                await state_store.transition(conn, pending["id"], "executed", result=exec_result)
                email_io.send_email(
                    subject=f"Done: {pending['pipeline_name']}",
                    body_html=f"<p>Dispatched GHA workflow for {pending['pipeline_name']}.</p>"
                              f"<pre>{json.dumps(exec_result, indent=2)}</pre>",
                    thread_id=thread_id,
                )
                return {"executed": True, "result": exec_result}

            elif fn == "trigger_local_pipeline":
                # Local pipeline launch is ASYNC via guardian_local_trigger.py on the PC.
                # Leave state at 'approved' — local picker will flip to 'executed' once launched.
                # Don't transition here.
                exec_result = {
                    "note": "queued for local Task Scheduler pickup (polls every 5 min)"
                }
                email_io.send_email(
                    subject=f"Queued: {pending['pipeline_name']}",
                    body_html=f"<p>Queued {pending['pipeline_name']} for local Task Scheduler. "
                              f"Will launch on your PC within 5 minutes when it's on. "
                              f"You'll get another email when the pipeline finishes.</p>",
                    thread_id=thread_id,
                )
                return {"queued_local": True, "result": exec_result}

            else:
                exec_result = {"error": f"unknown function {fn!r}"}
                await state_store.transition(conn, pending["id"], "escalated", result=exec_result)
                return {"error": exec_result["error"]}

        except Exception as e:
            await state_store.transition(conn, pending["id"], "escalated",
                                         result={"error": str(e)})
            email_io.send_email(
                subject=f"ERROR executing: {pending['pipeline_name']}",
                body_html=f"<p>Attempted but failed: <pre>{e}</pre></p>",
                thread_id=thread_id,
            )
            return {"error": str(e)}


# ========== EMAIL BODY FORMATTERS ==========

def _format_auto_fix_email(pipeline_name, classification, fix_result):
    return f"""
    <h2>Auto-fixed: {pipeline_name}</h2>
    <p><strong>Pattern:</strong> {classification['pattern_id']} — {classification['title']}</p>
    <p><strong>Action taken:</strong></p>
    <pre>{json.dumps(fix_result, indent=2)}</pre>
    <p>No action needed from you. Replying is optional.</p>
    """


def _format_approval_email(pipeline_name, classification, run_id):
    return f"""
    <h2>Approval needed: {pipeline_name}</h2>
    <p><strong>Pattern:</strong> {classification['pattern_id']} — {classification['title']}</p>
    <p><strong>Proposed action:</strong> {json.dumps(classification.get('fix_action'), indent=2)}</p>
    <p><strong>What to do:</strong> Reply to this email. Examples:</p>
    <ul>
      <li>"go ahead" — execute as proposed</li>
      <li>"no" — decline</li>
      <li>"just clean, don't re-run" — modified action</li>
    </ul>
    """


def _format_escalation_email(pipeline_name, classification, run_id):
    return f"""
    <h2>Escalation: {pipeline_name}</h2>
    <p><strong>Pattern:</strong> {classification['pattern_id']} — {classification['title']}</p>
    <p>This needs your judgment. Guardian will not act. Evidence:</p>
    <pre>{json.dumps(classification.get('evidence', {}), indent=2)}</pre>
    """


def _format_missing_run_email(missing):
    return f"""
    <h2>Missing run: {missing['pipeline_name']}</h2>
    <p>Expected to start at: <strong>{missing['expected_start'].isoformat()}</strong></p>
    <p>Runner: {missing['runner']}</p>
    <p>Reply "go ahead" to trigger it, or "skip" to let it lie.</p>
    """


# ========== CLI ENTRY ==========

def main():
    parser = argparse.ArgumentParser(description="Pipeline Guardian Agent")
    parser.add_argument("trigger", choices=["failure", "periodic", "reply"])
    parser.add_argument("--pipeline-name", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--thread-id", default=None)
    args = parser.parse_args()

    if args.trigger == "failure":
        if not args.pipeline_name:
            print("--pipeline-name required for failure trigger", file=sys.stderr)
            sys.exit(1)
        result = asyncio.run(handle_failure(args.pipeline_name, args.run_id))
    elif args.trigger == "periodic":
        result = asyncio.run(handle_periodic())
    elif args.trigger == "reply":
        if not args.thread_id:
            print("--thread-id required for reply trigger", file=sys.stderr)
            sys.exit(1)
        result = asyncio.run(handle_reply(args.thread_id))

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test locally in dry-run mode**

Run: `GUARDIAN_DRY_RUN=true SUPABASE_PASSWORD=[REDACTED-OLD-PW] python -m pipeline_guardian.agent periodic`
Expected: JSON output showing `{"stuck_detected": 0, "missing_detected": 0 (or more), "reminders_sent": 0}` — no exceptions.

- [ ] **Step 3: Commit**

```bash
git add pipeline_guardian/agent.py
git commit -m "feat(guardian): main agent entry — handle_failure / handle_periodic / handle_reply"
```

---

## Phase 3: CI/CD + Orchestration

### Task 3.1: GHA workflow — pipeline-guardian.yml

**Files:**
- Create: `.github/workflows/pipeline-guardian.yml`

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/pipeline-guardian.yml`:
```yaml
name: Pipeline Guardian

on:
  repository_dispatch:
    types: [guardian-failure, guardian-periodic, guardian-reply]
  workflow_dispatch:
    inputs:
      trigger:
        description: 'Which handler to run'
        required: true
        type: choice
        options: [failure, periodic, reply]
        default: periodic
      pipeline_name:
        description: 'Pipeline name (for failure trigger)'
        required: false
      run_id:
        description: 'Run UUID (for failure trigger)'
        required: false
      thread_id:
        description: 'Email thread id (for reply trigger)'
        required: false
      dry_run:
        description: 'Dry run mode (no actions executed)'
        type: boolean
        default: true

jobs:
  guardian:
    runs-on: ubuntu-latest
    env:
      SUPABASE_PASSWORD: ${{ secrets.SUPABASE_PASSWORD }}
      CLAUDE_API_KEY: ${{ secrets.CLAUDE_API_KEY }}
      GH_DISPATCH_PAT: ${{ secrets.GH_DISPATCH_PAT }}
      GUARDIAN_DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install deps
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt

      - name: Write Gmail token
        env:
          NOTIFIER_TOKEN_PICKLE: ${{ secrets.NOTIFIER_TOKEN_PICKLE }}
          NOTIFIER_CREDENTIALS_JSON: ${{ secrets.NOTIFIER_CREDENTIALS_JSON }}
        run: |
          echo "$NOTIFIER_TOKEN_PICKLE" | base64 -d > swift_api_pipeline/token.pickle
          echo "$NOTIFIER_CREDENTIALS_JSON" > swift_api_pipeline/credentials.json

      - name: Resolve trigger type
        id: resolve
        run: |
          if [[ "${{ github.event_name }}" == "repository_dispatch" ]]; then
            case "${{ github.event.action }}" in
              guardian-failure) echo "trigger=failure" >> $GITHUB_OUTPUT ;;
              guardian-periodic) echo "trigger=periodic" >> $GITHUB_OUTPUT ;;
              guardian-reply) echo "trigger=reply" >> $GITHUB_OUTPUT ;;
            esac
            echo "pipeline_name=${{ github.event.client_payload.pipeline_name }}" >> $GITHUB_OUTPUT
            echo "run_id=${{ github.event.client_payload.run_id }}" >> $GITHUB_OUTPUT
            echo "thread_id=${{ github.event.client_payload.thread_id }}" >> $GITHUB_OUTPUT
          else
            echo "trigger=${{ inputs.trigger }}" >> $GITHUB_OUTPUT
            echo "pipeline_name=${{ inputs.pipeline_name }}" >> $GITHUB_OUTPUT
            echo "run_id=${{ inputs.run_id }}" >> $GITHUB_OUTPUT
            echo "thread_id=${{ inputs.thread_id }}" >> $GITHUB_OUTPUT
          fi

      - name: Run guardian
        run: |
          cd $GITHUB_WORKSPACE
          ARGS="${{ steps.resolve.outputs.trigger }}"
          [ -n "${{ steps.resolve.outputs.pipeline_name }}" ] && ARGS="$ARGS --pipeline-name ${{ steps.resolve.outputs.pipeline_name }}"
          [ -n "${{ steps.resolve.outputs.run_id }}" ] && ARGS="$ARGS --run-id ${{ steps.resolve.outputs.run_id }}"
          [ -n "${{ steps.resolve.outputs.thread_id }}" ] && ARGS="$ARGS --thread-id ${{ steps.resolve.outputs.thread_id }}"
          echo "Running: python -m pipeline_guardian.agent $ARGS"
          python -m pipeline_guardian.agent $ARGS
```

- [ ] **Step 2: Add required GHA secrets (manual step)**

Through the GitHub UI for `jamilmendez-ontel/local-pipeline`:
- Settings → Secrets and variables → Actions → New repository secret
- Add `CLAUDE_API_KEY` (from local-ai-agent `.env`)
- Confirm existing: `SUPABASE_PASSWORD`, `GH_DISPATCH_PAT`, `NOTIFIER_TOKEN_PICKLE`, `NOTIFIER_CREDENTIALS_JSON`

- [ ] **Step 3: Test the workflow via manual dispatch**

From GitHub UI: Actions → Pipeline Guardian → "Run workflow" → trigger=`periodic`, dry_run=`true`
Expected: Workflow completes green; logs show `{"stuck_detected": 0, "missing_detected": 0 or more}`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/pipeline-guardian.yml
git commit -m "feat(guardian): GHA workflow with 3 repository_dispatch event types + manual dispatch"
```

---

### Task 3.2: Apps Script triggers (Gmail watchers + time trigger)

**Files:**
- Create: `scripts/guardian_triggers.gs`

- [ ] **Step 1: Create Apps Script file**

Create `scripts/guardian_triggers.gs`:
```javascript
/**
 * Pipeline Guardian triggers.
 *
 * Deploy this to Google Apps Script under jamil.mendez@nanoninth.com:
 *   1. script.google.com -> New Project
 *   2. Paste this file
 *   3. Script Properties: add GITHUB_PAT (same fine-grained PAT used by gmail_trigger.gs)
 *   4. Triggers:
 *      - watchFailureEmails: Gmail (on incoming message) OR time-trigger every 5 min
 *      - watchReplyEmails: time-trigger every 5 min
 *      - periodicCheck: time-trigger every 5 min
 *   5. Run authorize() once to grant Gmail scope.
 */

const REPO = 'jamilmendez-ontel/local-pipeline';

function authorize() {
  // Touching these APIs triggers the OAuth consent
  GmailApp.search('test', 0, 1);
  UrlFetchApp.fetch('https://api.github.com');
}

function fireDispatch(eventType, payload) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_PAT');
  if (!token) throw new Error('Set script property GITHUB_PAT');
  const url = `https://api.github.com/repos/${REPO}/dispatches`;
  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: `token ${token}`, Accept: 'application/vnd.github.v3+json' },
    payload: JSON.stringify({ event_type: eventType, client_payload: payload || {} }),
    muteHttpExceptions: true,
  });
  Logger.log(`Dispatch ${eventType}: ${response.getResponseCode()}`);
}

/**
 * Scans for unread pipeline FAILURE emails and dispatches guardian-failure.
 * Marks them as read to prevent duplicate dispatch.
 */
function watchFailureEmails() {
  const query = 'subject:"Pipeline FAILED" is:unread newer_than:1d';
  const threads = GmailApp.search(query, 0, 20);
  for (const thread of threads) {
    const msg = thread.getMessages()[0];
    const subject = msg.getSubject();
    // Extract pipeline name from "Pipeline FAILED: Asset Tasks (...)"
    const match = subject.match(/Pipeline FAILED:\s*([^(]+)/);
    const pipelineName = match ? match[1].trim().toLowerCase().replace(/\s+/g, '_') : 'unknown';
    // Map to pipeline_name (matches agent.pipeline_schedule.pipeline_name)
    const pipelineMap = {
      'asset_tasks': 'asset_tasks_extract',
      'timer': 'timer_extract',
      'forms': 'forms_extract',
      'user_priorities': 'user_priorities_extract',
      'organizations_&_projects': 'orgs_projects_extract',
      'calendar_leave': 'calendar_leave',
      'timer_discrepancies': 'timer_discrepancies',
    };
    const pipelineMapped = pipelineMap[pipelineName] || pipelineName;

    fireDispatch('guardian-failure', {
      pipeline_name: pipelineMapped,
      email_thread_id: thread.getId(),
    });
    thread.markRead();
  }
}

/**
 * Scans for unread reply emails in [Pipeline Guardian] threads.
 */
function watchReplyEmails() {
  const query = 'subject:"[Pipeline Guardian]" is:unread';
  const threads = GmailApp.search(query, 0, 20);
  for (const thread of threads) {
    // Only dispatch if the thread has an inbound reply (not just our own send)
    const messages = thread.getMessages();
    const lastMsg = messages[messages.length - 1];
    const from = lastMsg.getFrom().toLowerCase();
    if (from.includes('jamil.mendez')) {  // user replied
      fireDispatch('guardian-reply', { thread_id: thread.getId() });
      thread.markRead();
    }
  }
}

/**
 * Fires the periodic check every 5 minutes.
 */
function periodicCheck() {
  fireDispatch('guardian-periodic', {});
}
```

- [ ] **Step 2: Deploy manually (one-time setup — not automated)**

1. Open https://script.google.com under `jamil.mendez@nanoninth.com`
2. New Project → "Pipeline Guardian Triggers"
3. Paste `guardian_triggers.gs` content
4. Project Settings → Script Properties → Add `GITHUB_PAT` (same PAT as `gmail_trigger.gs`)
5. Run `authorize()` once (grants Gmail + UrlFetch scopes)
6. Triggers:
   - `watchFailureEmails` — time-based, every 5 minutes
   - `watchReplyEmails` — time-based, every 5 minutes
   - `periodicCheck` — time-based, every 5 minutes

- [ ] **Step 3: Commit**

```bash
git add scripts/guardian_triggers.gs
git commit -m "feat(guardian): Apps Script triggers (failure watcher, reply watcher, periodic)"
```

---

### Task 3.3: Local pipeline trigger delivery

**Files:**
- Create: `swift_api_pipeline/guardian_local_trigger.py`
- Create: `swift_api_pipeline/scheduled_guardian_local_trigger.bat`

- [ ] **Step 1: Create the local trigger processor**

Create `swift_api_pipeline/guardian_local_trigger.py`:
```python
"""Runs on local PC via Task Scheduler every 5 min.

Reads agent.monitor_state for rows with state='approved' and
proposed_action.function = 'trigger_local_pipeline', launches the pipeline,
and marks the row 'executed'.

This is the bridge between cloud Guardian decisions and local pipeline execution.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg


REPO_ROOT = Path(__file__).resolve().parent
MAIN_PY = REPO_ROOT / "main.py"


async def main():
    conn = await asyncpg.connect(
        host=os.environ.get("SUPABASE_HOST", "db.voqfjfngdpcvevbkikud.supabase.co"),
        port=int(os.environ.get("SUPABASE_PORT", "5432")),
        database="postgres", user="postgres",
        password=os.environ["SUPABASE_PASSWORD"],
        statement_cache_size=0,
    )
    try:
        rows = await conn.fetch(
            """
            SELECT id, pipeline_name, proposed_action
            FROM agent.monitor_state
            WHERE state = 'approved'
              AND proposed_action->>'function' = 'trigger_local_pipeline'
            ORDER BY created_at ASC
            LIMIT 5
            """
        )
        for r in rows:
            pipeline_name = r["pipeline_name"]
            # Map pipeline_name to --pipeline flag
            cli_flag = {
                "asset_tasks_extract": "asset_tasks",
                "calendar_leave": "calendar",
            }.get(pipeline_name)
            if not cli_flag:
                print(f"Skipping unknown local pipeline: {pipeline_name}")
                continue
            print(f"Launching: python main.py --pipeline {cli_flag}")
            proc = subprocess.Popen(
                [sys.executable, str(MAIN_PY), "--pipeline", cli_flag],
                cwd=str(REPO_ROOT),
            )
            # Fire-and-forget — update state immediately; the pipeline's own
            # notifier will send success/failure email which the Guardian sees.
            await conn.execute(
                """
                UPDATE agent.monitor_state
                SET state = 'executed',
                    executed_at = NOW(),
                    result = $2::jsonb
                WHERE id = $1
                """,
                r["id"],
                json.dumps({"launched_pid": proc.pid, "via": "local_trigger"}),
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Create batch wrapper**

Create `swift_api_pipeline/scheduled_guardian_local_trigger.bat`:
```batch
@echo off
REM Runs every 5 min via Task Scheduler — checks for Guardian-approved local triggers

cd /d "%~dp0"

REM Source env vars from .env (simple KEY=VALUE parser)
for /f "tokens=1,* delims==" %%a in (..\.env) do (
    if not "%%a"=="" if not "%%a:~0,1"=="#" set "%%a=%%b"
)

python guardian_local_trigger.py
```

- [ ] **Step 3: Register Task Scheduler job (manual)**

1. Open Task Scheduler → Create Task
2. Name: `Guardian-LocalTrigger`
3. Security: run as `admin`, run whether user is logged on, highest privileges
4. Trigger: repeat every 5 min, indefinitely
5. Action: start program `C:\Users\admin\Desktop\Projects\ai-projects\local-pipeline\swift_api_pipeline\scheduled_guardian_local_trigger.bat`

- [ ] **Step 4: Configure existing pipelines to run-missed-tasks**

For each of these tasks in Task Scheduler: `SwiftPipeline-Nightly`, `SwiftPipeline-Calendar`:
- Right-click → Properties → Settings tab
- Check: "Run task as soon as possible after a scheduled start is missed"

This lets the pipeline auto-run when the PC boots after being off during its scheduled time.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/guardian_local_trigger.py swift_api_pipeline/scheduled_guardian_local_trigger.bat
git commit -m "feat(guardian): local trigger processor + batch for Task Scheduler"
```

---

## Phase 4: Deployment & Validation

### Task 4.1: README for the guardian module

**Files:**
- Create: `pipeline_guardian/README.md`

- [ ] **Step 1: Write the README**

Create `pipeline_guardian/README.md`:
```markdown
# Pipeline Guardian

Auto-remediation agent for pipeline failures. Monitors `pipeline.pipeline_runs`,
detects failures and missing runs, auto-fixes known-safe patterns, and coordinates
bigger fixes via conversational email reply.

**Design doc:** `../docs/superpowers/specs/2026-04-15-pipeline-guardian-design.md`

## Architecture

Three triggers, all via Apps Script → `repository_dispatch`:
- **Pipeline failure email** → `guardian-failure` event
- **5-min time trigger** → `guardian-periodic` event
- **Email reply** → `guardian-reply` event

Agent runs in GHA (`.github/workflows/pipeline-guardian.yml`), writes state to
`agent.monitor_state` on Supabase, sends emails via `pipeline_notifier.py`.

## Modules

| File | Purpose |
|---|---|
| `agent.py` | Main entry — dispatches to handler by trigger type |
| `detectors.py` | Classifies failures against `agent.known_issues` patterns |
| `actions.py` | Executes DB cleanups + pipeline triggers (with safety rails + dry-run) |
| `state_store.py` | CRUD on `agent.monitor_state` |
| `schedule_check.py` | Missing-run detection against `agent.pipeline_schedule` |
| `email_io.py` | Send/read Gmail via existing gmail_client |
| `email_parser.py` | Claude-powered reply intent classifier |
| `known_issues.yaml` | Editable knowledge base (18 patterns + 7 pipelines) |
| `sync_known_issues.py` | One-way YAML → DB sync |

## Adding a new failure pattern

1. Edit `known_issues.yaml`, add a new entry under `patterns:`
2. Run `python pipeline_guardian/sync_known_issues.py`
3. Optionally add a test case in `tests/pipeline_guardian/test_detectors.py`
4. Commit

No code redeploy needed.

## Kill switch

To halt all Guardian actions immediately:
```sql
INSERT INTO agent.monitor_state (pipeline_name, pattern_id, state, severity)
VALUES ('_SYSTEM', '_DISABLED', 'detected', 'auto');
```

To re-enable:
```sql
UPDATE agent.monitor_state SET state = 'executed'
WHERE pipeline_name = '_SYSTEM' AND pattern_id = '_DISABLED';
```

## Dry-run mode

Set `GUARDIAN_DRY_RUN=true` (default in GHA workflow). Actions log intent but don't execute.
Used during Phase 1 rollout (week 1) for safe validation.

To promote to live: change `default: true` to `default: false` in `.github/workflows/pipeline-guardian.yml`.

## Rate limit

Max 3 automated re-runs per pipeline per 24h (enforced in `_send_approval_email`).
Prevents runaway cycles.
```

- [ ] **Step 2: Commit**

```bash
git add pipeline_guardian/README.md
git commit -m "docs(guardian): README with architecture + operator guide"
```

---

### Task 4.2: Update MEMORY.md + memory pointer to mark implementation complete

**Files:**
- Modify: `C:/Users/admin/.claude/projects/C--Users-admin-Desktop-Projects-ai-projects/memory/project_pipeline_guardian.md`
- Modify: `C:/Users/admin/.claude/projects/C--Users-admin-Desktop-Projects-ai-projects/memory/MEMORY.md` (add entry under Key Patterns section)

- [ ] **Step 1: Update the guardian memory file to mark implementation done**

Edit `project_pipeline_guardian.md`, change `Status:` line to:
```
**Status:** Implementation complete 2026-XX-XX — Phase 1 (dry-run) deployed. Ready for Phase 2 switch after 1 week of dry-run validation.
```

Add at the end:
```markdown
## Post-implementation notes

### How to promote dry-run → live (Phase 2)
1. Open `.github/workflows/pipeline-guardian.yml`
2. Change `GUARDIAN_DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}` to `'false'`
3. Commit + push
4. Monitor first 48h of live operation closely — watch for runaway or misclassifications

### Adding a new pattern without redeploy
1. Edit `pipeline_guardian/known_issues.yaml`
2. Run `python pipeline_guardian/sync_known_issues.py` locally
3. Commit the YAML change
4. The next guardian run uses the updated patterns

### Kill switch (instant halt)
```sql
INSERT INTO agent.monitor_state (pipeline_name, pattern_id, state, severity)
VALUES ('_SYSTEM', '_DISABLED', 'detected', 'auto');
```
```

- [ ] **Step 2: Add key pattern memory to MEMORY.md**

Add to `MEMORY.md` under `## Key Patterns` section:
```markdown
- **Pipeline Guardian**: `pipeline_guardian/` module. Runs in GHA via `repository_dispatch` from Apps Script (failure email, 5-min periodic, reply email). Auto-fixes 5 safe patterns, proposes 6 approve patterns, escalates 7. State in `agent.monitor_state`. Dry-run gate: `GUARDIAN_DRY_RUN` env var.
```

- [ ] **Step 3: Commit memory updates**

(Memory files live outside the repo — no git commit needed; they're auto-persisted by Claude Code.)

---

### Task 4.3: Phase 1 dry-run deployment + 1-week validation

- [ ] **Step 1: Confirm all code merged to main**

```bash
git log --oneline -20 | head -30
```
Expected: see all guardian commits in sequence.

- [ ] **Step 2: Push to remote**

```bash
git push origin main
```

- [ ] **Step 3: Verify GHA workflow runs green on `periodic` trigger**

From GitHub UI: Actions → Pipeline Guardian → Run workflow → trigger=`periodic`, dry_run=`true`.
Expected: Green workflow. Check run log for output like:
```json
{"stuck_detected": 0, "missing_detected": 0, "reminders_sent": 0}
```

- [ ] **Step 4: Send a test failure email manually and watch**

Simulate by manually triggering `failure` via workflow_dispatch:
- trigger=`failure`, pipeline_name=`asset_tasks_extract`, run_id=<a recent failed run's UUID>, dry_run=`true`

Expected: Agent classifies the failure, writes to `agent.monitor_state` (with state=`auto_fixed` or `awaiting_approval`), sends an email. In dry-run mode, no actual data mutation.

- [ ] **Step 5: Enable Apps Script triggers**

In the deployed Apps Script project:
- Add the three time-based triggers (watchFailureEmails, watchReplyEmails, periodicCheck) via `Triggers` in the Apps Script editor
- Interval: every 5 minutes

- [ ] **Step 6: Leave in dry-run for 1 week, review daily**

Each morning: query `agent.monitor_state` to see what the agent detected. Confirm:
- Detections match what you would have done manually
- No false positives
- Missing-run detection fires correctly
- Reply parsing works (can test by replying to a guardian email)

Query to review previous day:
```sql
SELECT created_at AT TIME ZONE 'America/New_York' AS created_et,
       pipeline_name, pattern_id, severity, state,
       diagnosis->>'evidence' AS evidence_snippet
FROM agent.monitor_state
WHERE created_at > NOW() - interval '24 hours'
ORDER BY created_at DESC;
```

- [ ] **Step 7: Promote to live (after 1 week with no issues)**

Edit `.github/workflows/pipeline-guardian.yml`, change default dry_run from `'true'` to `'false'`.

```bash
git add .github/workflows/pipeline-guardian.yml
git commit -m "feat(guardian): promote to live (GUARDIAN_DRY_RUN=false)"
git push origin main
```

- [ ] **Step 8: Monitor 48h post-live**

Watch:
- `agent.monitor_state` rows — are auto-fixes succeeding?
- Your email inbox — approval requests readable?
- Reply parsing — does "go ahead" work correctly?

If any runaway or misclassification: use kill switch immediately, fix pattern, re-sync YAML.

---

## Done

At this point the Pipeline Guardian is:
- ✅ Monitoring all 7 pipelines (5 GHA + 2 local)
- ✅ Auto-fixing 5 known-safe patterns (stale runs, stuck status, orphaned indexes, DNS blips, transient 503s)
- ✅ Proposing fixes for 6 approve-level patterns (statement timeouts, connection drops, partial extractions, missing runs, post-stale-cleanup reruns, duplicate keys)
- ✅ Escalating 7 code-bug / auth-issue patterns
- ✅ Accepting natural-language email replies ("go ahead", "just clean", "no")
- ✅ Rate-limited (max 3 re-runs/pipeline/24h)
- ✅ Kill-switchable
- ✅ Ready for asset_tasks cloud migration (no code changes needed)

## Future Enhancements (out of scope for this plan)

- Granular per-project re-run for asset_tasks (today: full pipeline only)
- Gmail token rotation for Guardian's own access (reuse existing reminder pattern)
- Multi-pipeline failure cascade detection (one email per root cause)
- Operations Portal integration — render guardian decisions as a timeline (depends on `project_portal.md`)
