# Revenue Rate Sheet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the 239-row Revenue Metrics rate sheet from `Revenue Metrics_Duration & Amount (1).xlsx` into a new Supabase table `reference.ref_task_revenue_rates`, so revenue analytics and DARA can look up `(market_bucket, task_name) → (duration_hrs, amount_usd)`.

**Architecture:** Static reference table in the `reference` schema (alongside `ref_employees`, `ref_nni_directory`, `ref_ontel_techops_projects`). A one-time Python helper reads the xlsx and emits a SQL migration file. The migration is applied via the Supabase MCP `apply_migration` tool. No FK to other tables — `market_bucket` is text-only; Market → project_did mapping is deferred.

**Tech Stack:** Python 3 (openpyxl) for SQL generation, PostgreSQL 15 (Supabase), Supabase MCP tools (`apply_migration`, `execute_sql`).

**Spec:** `docs/specs/revenue-rate-sheet.md`

---

## File Structure

All artifacts live next to the spec so they stay tied to the design:

- **Create**: `docs/specs/revenue-rate-sheet-artifacts/generate_migration.py` — Python helper that reads xlsx → emits SQL.
- **Create**: `docs/specs/revenue-rate-sheet-artifacts/migration_revenue_rates.sql` — generated migration body (committed alongside the helper for traceability).
- **DB object**: `reference.ref_task_revenue_rates` (created in Supabase via `apply_migration`).

Note: the workspace root is not a git repo, so there is no `git add`/`git commit` step. If you want the artifacts under version control, copy them into `local-pipeline/` (which is a git repo) after the load.

---

## Task 1: Write the migration generator script

**Files:**
- Create: `docs/specs/revenue-rate-sheet-artifacts/generate_migration.py`

- [ ] **Step 1: Verify the source xlsx is present**

Run:
```powershell
Get-Item "C:\Users\admin\Downloads\Revenue Metrics_Duration & Amount (1).xlsx" | Select-Object Name, Length
```

Expected: file is listed (~10–20 KB). If missing, ask the user for the file location before continuing.

- [ ] **Step 2: Create the artifacts directory**

Run:
```powershell
New-Item -ItemType Directory -Force -Path "C:\Users\admin\Desktop\Projects\ai-projects\docs\superpowers\specs\2026-05-12-revenue-rate-sheet-artifacts" | Out-Null
```

Expected: no output, directory exists afterward.

- [ ] **Step 3: Write the generator script**

Create `docs/specs/revenue-rate-sheet-artifacts/generate_migration.py`:

```python
"""
One-shot generator: reads the Revenue Metrics xlsx and emits a SQL migration
that creates reference.ref_task_revenue_rates and inserts all rows.

Run:
    python generate_migration.py

Outputs migration_revenue_rates.sql next to this script.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import openpyxl

SOURCE_XLSX = Path(r"C:\Users\admin\Downloads\Revenue Metrics_Duration & Amount (1).xlsx")
OUTPUT_SQL = Path(__file__).parent / "migration_revenue_rates.sql"
SOURCE_FILE_LABEL = SOURCE_XLSX.name

PREFIX_RE = re.compile(r"^\d+\.\s+")


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    wb = openpyxl.load_workbook(SOURCE_XLSX, data_only=True)
    ws = wb["Sheet1"]

    rows: list[tuple[str, str, str, Decimal, Decimal]] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            # Header row: Market/Project, Task, Duration Hrs, Amount
            continue
        market, task, dur, amt = row[0], row[1], row[2], row[3]
        if market is None or task is None:
            continue
        market = str(market).strip()
        task = str(task).strip()
        task_norm = PREFIX_RE.sub("", task)
        dur = Decimal(str(dur if dur is not None else 0))
        amt = Decimal(str(amt if amt is not None else 0))
        rows.append((market, task, task_norm, dur, amt))

    lines: list[str] = []
    lines.append("-- Migration: add_ref_task_revenue_rates")
    lines.append("-- Source: " + SOURCE_FILE_LABEL)
    lines.append("-- Spec: docs/specs/revenue-rate-sheet.md")
    lines.append("")
    lines.append("CREATE TABLE IF NOT EXISTS reference.ref_task_revenue_rates (")
    lines.append("  id              bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,")
    lines.append("  market_bucket   text NOT NULL,")
    lines.append("  task_name       text NOT NULL,")
    lines.append("  task_name_norm  text NOT NULL,")
    lines.append("  duration_hrs    numeric(5,2) NOT NULL,")
    lines.append("  amount_usd      numeric(10,2) NOT NULL,")
    lines.append("  source_file     text,")
    lines.append("  created_at      timestamptz DEFAULT now(),")
    lines.append("  updated_at      timestamptz DEFAULT now(),")
    lines.append("  UNIQUE (market_bucket, task_name)")
    lines.append(");")
    lines.append("")
    lines.append("CREATE INDEX IF NOT EXISTS idx_revenue_rates_market")
    lines.append("  ON reference.ref_task_revenue_rates(market_bucket);")
    lines.append("CREATE INDEX IF NOT EXISTS idx_revenue_rates_task_norm")
    lines.append("  ON reference.ref_task_revenue_rates(task_name_norm);")
    lines.append("")
    lines.append(
        "INSERT INTO reference.ref_task_revenue_rates "
        "(market_bucket, task_name, task_name_norm, duration_hrs, amount_usd, source_file) VALUES"
    )

    value_lines = []
    for market, task, task_norm, dur, amt in rows:
        value_lines.append(
            f"  ({sql_str(market)}, {sql_str(task)}, {sql_str(task_norm)}, "
            f"{dur}, {amt}, {sql_str(SOURCE_FILE_LABEL)})"
        )
    lines.append(",\n".join(value_lines) + ";")
    lines.append("")

    OUTPUT_SQL.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {OUTPUT_SQL}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the generator**

Run:
```powershell
python "C:\Users\admin\Desktop\Projects\ai-projects\docs\superpowers\specs\2026-05-12-revenue-rate-sheet-artifacts\generate_migration.py"
```

Expected: `Wrote 239 rows to ...migration_revenue_rates.sql`. If the row count is not 239, stop and investigate before continuing — the xlsx may have been edited.

- [ ] **Step 5: Spot-check the generated SQL**

Run:
```powershell
$sql = "C:\Users\admin\Desktop\Projects\ai-projects\docs\superpowers\specs\2026-05-12-revenue-rate-sheet-artifacts\migration_revenue_rates.sql"
(Get-Content $sql | Measure-Object -Line).Lines
Get-Content $sql -TotalCount 25
Get-Content $sql -Tail 5
```

Expected:
- Line count is roughly `239 + ~20` (≈259).
- First lines show the `CREATE TABLE` and `CREATE INDEX` statements.
- Last line ends with `;`.
- Inside the VALUES list, look for at least one row with `task_name` containing a numeric prefix (e.g., `'1. RF Mitigation COP Complete'`) whose `task_name_norm` is the same string without the `1. ` prefix.

---

## Task 2: Apply the migration to Supabase

**Files:**
- Read: `docs/specs/revenue-rate-sheet-artifacts/migration_revenue_rates.sql`

- [ ] **Step 1: Confirm `reference` schema exists and current ref tables**

Use the Supabase MCP `execute_sql` tool against project `voqfjfngdpcvevbkikud`:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'reference'
ORDER BY table_name;
```

Expected: returns at least `ref_employees`, `ref_nni_directory`, `ref_ontel_techops_projects`. **Must NOT** include `ref_task_revenue_rates` — if it does, the table already exists and you should stop and ask the user how to proceed (drop + reload, or skip).

- [ ] **Step 2: Read the migration body**

Read the file:
```
docs/specs/revenue-rate-sheet-artifacts/migration_revenue_rates.sql
```

Keep its contents in scratch memory — you'll paste it into the migration call.

- [ ] **Step 3: Apply the migration**

Use the Supabase MCP `apply_migration` tool:
- `project_id`: `voqfjfngdpcvevbkikud`
- `name`: `add_ref_task_revenue_rates`
- `query`: contents of `migration_revenue_rates.sql`

Expected: success response. If it errors with a permission/RLS message, surface the error and ask the user before retrying.

- [ ] **Step 4: Confirm the table is registered as a migration**

Use the Supabase MCP `list_migrations` tool with `project_id: voqfjfngdpcvevbkikud`.

Expected: the latest entry is `add_ref_task_revenue_rates`.

---

## Task 3: Verify the loaded data

**Files:** none (verification only — run queries via Supabase MCP `execute_sql`).

- [ ] **Step 1: Row count matches xlsx**

```sql
SELECT COUNT(*) AS n FROM reference.ref_task_revenue_rates;
```

Expected: `n = 239`. If different, stop and investigate (likely a row was skipped during load).

- [ ] **Step 2: Distinct market bucket count is 14**

```sql
SELECT COUNT(DISTINCT market_bucket) AS n_buckets FROM reference.ref_task_revenue_rates;
```

Expected: `n_buckets = 14`.

- [ ] **Step 3: All 14 expected buckets present**

```sql
SELECT market_bucket, COUNT(*) AS n_tasks
FROM reference.ref_task_revenue_rates
GROUP BY market_bucket
ORDER BY market_bucket;
```

Expected: exactly these 14 buckets (any deviation → stop and investigate):
- `AAHI/DONOR`
- `AAHI/MDU`
- `AT&T`
- `Dish Wireless`
- `FTTH Phase 1`
- `FTTH Phase 2`
- `Ground Scope`
- `Gulf Services`
- `TMO`
- `USCC`
- `VZW Decom`
- `VZW Embedded / Macro`
- `VZW Small Cell`
- `Westell/CGC`

- [ ] **Step 4: Normalization stripped numeric prefixes**

```sql
SELECT task_name, task_name_norm
FROM reference.ref_task_revenue_rates
WHERE task_name ~ '^\d+\.\s'
LIMIT 5;
```

Expected: rows like `task_name = '1. RF Mitigation COP Complete'`, `task_name_norm = 'RF Mitigation COP Complete'`. If any row has the prefix still present in `task_name_norm`, the generator regex is wrong — fix and reload.

- [ ] **Step 5: No row has prefix left in `task_name_norm`**

```sql
SELECT COUNT(*) AS leaks
FROM reference.ref_task_revenue_rates
WHERE task_name_norm ~ '^\d+\.\s';
```

Expected: `leaks = 0`.

- [ ] **Step 6: Uniqueness constraint works**

```sql
SELECT market_bucket, task_name, COUNT(*) AS n
FROM reference.ref_task_revenue_rates
GROUP BY market_bucket, task_name
HAVING COUNT(*) > 1;
```

Expected: zero rows. (The UNIQUE constraint should have prevented duplicates, but this catches the case where the xlsx itself contained duplicates that the constraint silently rejected as a load error — if load succeeded with 239 rows, this should be empty.)

- [ ] **Step 7: Sample a recognizable row to spot-check values**

```sql
SELECT market_bucket, task_name, task_name_norm, duration_hrs, amount_usd
FROM reference.ref_task_revenue_rates
WHERE market_bucket = 'VZW Embedded / Macro'
  AND task_name = 'Final COP Complete';
```

Expected: `duration_hrs = 2.50`, `amount_usd = 100.00` (per the xlsx). If the numbers differ, the load picked up a different row — investigate before declaring success.

- [ ] **Step 8: Zero rows preserved**

```sql
SELECT COUNT(*) AS zero_rows
FROM reference.ref_task_revenue_rates
WHERE duration_hrs = 0 AND amount_usd = 0;
```

Expected: matches the count of zero/zero rows in the xlsx (run the same filter on the source file via Python beforehand, or accept whatever count returns and confirm it is non-zero — the spec explicitly preserves these).

---

## Task 4: Report completion and surface follow-ups

**Files:** none (communication only).

- [ ] **Step 1: Summarize for the user**

Report:
- Table `reference.ref_task_revenue_rates` created with 239 rows.
- Migration `add_ref_task_revenue_rates` registered.
- Sample value confirmed (VZW Embedded / Macro → Final COP Complete → 2.5h / $100).
- 14 market buckets loaded.

- [ ] **Step 2: Flag the deferred follow-ups from the spec**

Remind the user of "Out of Scope" items they may want to schedule:
- Market/Project → project_did mapping (table or column on `stg_projects`).
- DARA `agent.schema_metadata` row for this table (after confirming the scanner's schema coverage).
- An `analytics` view that joins this table to `stg_asset_tasks` for the eventual revenue dashboard.

- [ ] **Step 3: Update memory**

Update `C:\Users\admin\.claude\projects\C--Users-admin-Desktop-Projects-ai-projects\memory\project_revenue_rate_sheet.md` with the completion status (replace the "Open question" and "Next steps" sections with a short "Loaded on 2026-05-12" entry).

Update `MEMORY.md`'s Active Initiatives entry for the rate sheet to mark it as complete or move it to a "Completed" section.

---

## Self-Review Notes (author-only, can delete after read)

- **Spec coverage**: schema (Task 2), columns (Task 1 generator), zero-row preservation (Task 3 step 8), one-shot SQL migration (Task 2), `task_name_norm` precompute (Task 1, verified Task 3 steps 4–5), uniqueness (Task 3 step 6), no FK / no mapping (intentionally absent), DARA / mapping deferred (Task 4 step 2). ✓
- **Placeholders**: none.
- **Type consistency**: column names match between Task 1 SQL, Task 3 queries, and the spec.
- **Risk**: if the xlsx contains duplicate `(market, task)` pairs, the INSERT will fail entirely (single statement). If that happens, the generator needs an `ON CONFLICT DO NOTHING` or the xlsx needs dedup — flagged in Task 3 step 6 as a recovery branch.
