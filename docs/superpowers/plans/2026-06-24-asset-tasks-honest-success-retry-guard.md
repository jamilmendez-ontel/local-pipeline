# Asset-Tasks Honest-Success + Persistent Retry + Row-Count Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the asset-tasks pipeline send a SUCCESS email only on a genuinely clean run, retry failing projects up to 3 more rounds, and flag row-count regressions, while the successfully-extracted projects always still flow downstream.

**Architecture:** A new `PipelineOutcome` data carrier lets the extract function report a non-clean result *without raising*. `run_pipeline_with_notification` becomes three-way (clean→SUCCESS email, degraded→red email + exit 0, exception→FAILED + raise). The extract gains a 3-round retry loop and a per-project/total row-count guard whose baseline is persisted in the existing `pipeline_runs.metadata` jsonb (no migration).

**Tech Stack:** Python 3.12, asyncpg-style `db.execute`/`db.fetchrow` via `retry_db`, `concurrent.futures.ThreadPoolExecutor`, pytest (plain `test_*` functions under `swift_api_pipeline/tests/`), SMTP email via `pipeline_notifier`.

## Global Constraints

- Only touch our own schemas; this work reads/writes `pipeline.pipeline_runs` only. Verbatim DB rule: never touch `public`/`auth`/etc.
- Datetimes from Supabase are UTC; display in `America/New_York` (notifier already does this via `TZ_EASTERN`).
- No new DB object / no migration — reuse the existing `pipeline_runs.metadata` jsonb column.
- No em-dash in copy/log strings; use period/comma/colon/parens.
- Do NOT change the DB `status` enum behavior: partial/abnormal runs still complete as `'success'` so the good projects' transforms resolve via `WHERE status='success'`. Honesty lives in the email + the `error_message` note.
- New constants: `MAX_RETRY_ROUNDS = 3`, `RETRY_WAIT_SECONDS = 300` (existing), `ABNORMAL_DROP_PCT = 0.10`.

---

## File Structure

| File | Responsibility / change |
|------|------|
| `swift_api_pipeline/pipeline_notifier.py` | Add `PipelineOutcome` dataclass (status precedence + `email_status()`). No HTML changes (red rendering already keys off non-`SUCCESS`). |
| `swift_api_pipeline/extract_asset_tasks.py` | Constants; pure `detect_abnormal_counts`; `_retry_failed_once` seam + 3-round loop; build + return `PipelineOutcome`; persist counts; write error note on abnormal. |
| `swift_api_pipeline/base_extractor.py` | `complete_pipeline_run` gains `project_counts` param, merged into `metadata` jsonb; add `get_previous_project_counts`. |
| `swift_api_pipeline/main.py` | Three-way `run_pipeline_with_notification`; propagate `PipelineOutcome` through `run_asset_tasks_pipeline` / `run_asset_tasks_extract_pipeline`. |
| `swift_api_pipeline/tests/test_asset_tasks_resilience.py` | New test module (pytest). |

---

### Task 1: `PipelineOutcome` data carrier

**Files:**
- Modify: `swift_api_pipeline/pipeline_notifier.py` (add dataclass near `PipelineResult`, ~line 115)
- Test: `swift_api_pipeline/tests/test_asset_tasks_resilience.py`

**Interfaces:**
- Produces: `PipelineOutcome(run_id: str | None = None, failed_projects: list[str] = [], abnormal_projects: list[str] = [], detail: str = "")` with `.email_status() -> str` returning `"SUCCESS"`, `"PARTIAL FAILURE"`, or `"ABNORMAL ROW COUNT"`, and `.is_clean() -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# swift_api_pipeline/tests/test_asset_tasks_resilience.py
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pipeline_notifier import PipelineOutcome


def test_outcome_clean_when_no_problems():
    o = PipelineOutcome(run_id="r1")
    assert o.is_clean() is True
    assert o.email_status() == "SUCCESS"


def test_outcome_partial_takes_precedence_over_abnormal():
    o = PipelineOutcome(run_id="r1", failed_projects=["TS19"], abnormal_projects=["TS13"])
    assert o.is_clean() is False
    assert o.email_status() == "PARTIAL FAILURE"


def test_outcome_abnormal_when_only_counts_off():
    o = PipelineOutcome(run_id="r1", abnormal_projects=["TS13"])
    assert o.is_clean() is False
    assert o.email_status() == "ABNORMAL ROW COUNT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: FAIL with `ImportError: cannot import name 'PipelineOutcome'`

- [ ] **Step 3: Write minimal implementation**

Add after the `PipelineResult` dataclass in `pipeline_notifier.py` (it already imports `dataclass`; add `field` to that import if absent):

```python
@dataclass
class PipelineOutcome:
    """Returned by a pipeline function to report a non-clean result WITHOUT raising.

    Lets run_pipeline_with_notification choose SUCCESS vs a red degraded email
    while still letting the successfully-processed work flow downstream.
    """
    run_id: str = None
    failed_projects: list = field(default_factory=list)
    abnormal_projects: list = field(default_factory=list)
    detail: str = ""

    def is_clean(self) -> bool:
        return not self.failed_projects and not self.abnormal_projects

    def email_status(self) -> str:
        if self.failed_projects:
            return "PARTIAL FAILURE"
        if self.abnormal_projects:
            return "ABNORMAL ROW COUNT"
        return "SUCCESS"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/pipeline_notifier.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(notifier): add PipelineOutcome carrier for degraded-but-not-failed runs"
```

---

### Task 2: Pure `detect_abnormal_counts` helper + constants

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py` (constants near `RETRY_WAIT_SECONDS` line 29; helper at module scope)
- Test: `swift_api_pipeline/tests/test_asset_tasks_resilience.py`

**Interfaces:**
- Produces: `detect_abnormal_counts(current_counts: dict[str,int], baseline_counts: dict[str,int], drop_pct: float = ABNORMAL_DROP_PCT) -> list[str]` — returns sorted project names that are abnormal. Rules: a project is abnormal if its current count is `0`, OR (it has a positive baseline AND current < baseline*(1-drop_pct)). Projects absent from `baseline_counts` are skipped (no false alarm on first sight). Module constants `MAX_RETRY_ROUNDS = 3`, `ABNORMAL_DROP_PCT = 0.10`.

- [ ] **Step 1: Write the failing test**

```python
# append to swift_api_pipeline/tests/test_asset_tasks_resilience.py
from extract_asset_tasks import detect_abnormal_counts, ABNORMAL_DROP_PCT, MAX_RETRY_ROUNDS


def test_zero_rows_is_abnormal_even_with_no_baseline():
    assert detect_abnormal_counts({"TS13": 0}, {}) == ["TS13"]


def test_no_baseline_nonzero_is_skipped():
    assert detect_abnormal_counts({"TS13": 100}, {}) == []


def test_drop_beyond_tolerance_is_abnormal():
    # baseline 1000, current 850 -> 15% drop > 10%
    assert detect_abnormal_counts({"TS13": 850}, {"TS13": 1000}) == ["TS13"]


def test_small_dip_within_tolerance_is_clean():
    # baseline 1000, current 950 -> 5% drop <= 10%
    assert detect_abnormal_counts({"TS13": 950}, {"TS13": 1000}) == []


def test_growth_is_clean():
    assert detect_abnormal_counts({"TS13": 1200}, {"TS13": 1000}) == []


def test_multiple_projects_sorted():
    cur = {"TS19": 0, "TS13": 100, "TS16": 50}
    base = {"TS19": 500, "TS13": 100, "TS16": 1000}
    # TS19 zero -> abnormal; TS16 50 vs 1000 -> abnormal; TS13 flat -> clean
    assert detect_abnormal_counts(cur, base) == ["TS16", "TS19"]


def test_constants():
    assert MAX_RETRY_ROUNDS == 3
    assert ABNORMAL_DROP_PCT == 0.10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: FAIL with `ImportError: cannot import name 'detect_abnormal_counts'`

- [ ] **Step 3: Write minimal implementation**

Near the top of `extract_asset_tasks.py` (after `RETRY_WAIT_SECONDS = 300` on line 29):

```python
MAX_RETRY_ROUNDS = 3        # project-level retry rounds after the initial extraction
ABNORMAL_DROP_PCT = 0.10    # flag a project if its rows drop > 10% vs the previous successful run
```

At module scope (e.g. just below the constants):

```python
def detect_abnormal_counts(current_counts, baseline_counts, drop_pct=ABNORMAL_DROP_PCT):
    """Return sorted project names whose row count looks abnormal.

    A project is abnormal if it returned 0 rows, or (with a positive baseline)
    fell more than drop_pct below the previous successful run. Projects with no
    baseline and a nonzero count are skipped so first-ever extractions don't
    false-alarm.
    """
    abnormal = []
    for name, count in current_counts.items():
        if count == 0:
            abnormal.append(name)
            continue
        base = baseline_counts.get(name)
        if base and base > 0 and count < base * (1 - drop_pct):
            abnormal.append(name)
    return sorted(abnormal)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(asset-tasks): pure detect_abnormal_counts helper + retry/abnormal constants"
```

---

### Task 3: Persist baseline counts in `pipeline_runs.metadata`

**Files:**
- Modify: `swift_api_pipeline/base_extractor.py:86-97` (`complete_pipeline_run`); add `get_previous_project_counts`
- Test: `swift_api_pipeline/tests/test_asset_tasks_resilience.py`

**Interfaces:**
- Consumes: `self.db.execute(sql, *params)` and `self.db.fetchrow(sql, *params)` (existing), `self._pipeline_name`, `self.run_id`, module `SCHEMA_PIPELINE`, `retry_db`.
- Produces:
  - `complete_pipeline_run(self, status, records=None, error=None, project_counts: dict = None)` — when `project_counts` is provided, merges `{"project_counts": project_counts}` into the run's `metadata` jsonb.
  - `get_previous_project_counts(self) -> tuple[dict, int]` — returns `(project_counts, total_records)` from the most recent prior `status='success'` run for `self._pipeline_name` (excluding the current run), or `({}, 0)` if none.

- [ ] **Step 1: Write the failing test (fake db captures SQL + params)**

```python
# append to swift_api_pipeline/tests/test_asset_tasks_resilience.py
import json
from base_extractor import BaseExtractor  # adjust if the class name differs


class _FakeDB:
    def __init__(self, prev_row=None):
        self.executed = []          # list of (sql, params)
        self._prev_row = prev_row

    def execute(self, sql, *params):
        self.executed.append((sql, params))

    def fetchrow(self, sql, *params):
        return self._prev_row


def _make_extractor(fake_db):
    ex = BaseExtractor.__new__(BaseExtractor)   # bypass __init__/network
    ex.db = fake_db
    ex._pipeline_name = "asset_tasks_extract"
    ex.run_id = "run-current"
    return ex


def test_complete_pipeline_run_merges_project_counts_into_metadata():
    db = _FakeDB()
    ex = _make_extractor(db)
    ex.complete_pipeline_run("success", 100, error=None, project_counts={"TS13": 100})
    sql, params = db.executed[-1]
    assert "metadata" in sql and "::jsonb" in sql
    # the json payload param must contain project_counts
    assert any('"project_counts"' in p for p in params if isinstance(p, str))
    assert any('"TS13"' in p for p in params if isinstance(p, str))


def test_get_previous_project_counts_parses_metadata():
    db = _FakeDB(prev_row={"records_extracted": 900,
                           "project_counts": {"TS13": 500, "TS16": 400}})
    ex = _make_extractor(db)
    counts, total = ex.get_previous_project_counts()
    assert counts == {"TS13": 500, "TS16": 400}
    assert total == 900


def test_get_previous_project_counts_handles_no_prior_run():
    ex = _make_extractor(_FakeDB(prev_row=None))
    assert ex.get_previous_project_counts() == ({}, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -k previous_project or merges -v`
Expected: FAIL (`get_previous_project_counts` missing / `project_counts` not in SQL). If `BaseExtractor` import path differs, fix the import to the actual class name found in `base_extractor.py`.

- [ ] **Step 3: Write minimal implementation**

Replace `complete_pipeline_run` (base_extractor.py:86-97) with:

```python
    def complete_pipeline_run(self, status: str, records: int = None,
                              error: str = None, project_counts: dict = None) -> None:
        """Update pipeline run status on completion.

        When project_counts is supplied, merge {"project_counts": {...}} into the
        run's metadata jsonb so the next run can use it as a row-count baseline.
        """
        if project_counts is not None:
            payload = json.dumps({"project_counts": project_counts})
            retry_db(
                lambda: self.db.execute(
                    f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
                    f'SET status = $1, completed_at = $2, records_extracted = $3, '
                    f'error_message = $4, '
                    f'metadata = COALESCE(metadata, \'{{}}\'::jsonb) || $5::jsonb '
                    f'WHERE run_id = $6',
                    status, datetime.now(timezone.utc), records, error, payload,
                    str(self.run_id)
                ),
                description="update pipeline_runs (with counts)"
            )
        else:
            retry_db(
                lambda: self.db.execute(
                    f'UPDATE {SCHEMA_PIPELINE}.pipeline_runs '
                    f'SET status = $1, completed_at = $2, records_extracted = $3, '
                    f'error_message = $4 WHERE run_id = $5',
                    status, datetime.now(timezone.utc), records, error, str(self.run_id)
                ),
                description="update pipeline_runs"
            )
        logger.info(f"Pipeline run completed: {status}")

    def get_previous_project_counts(self):
        """Return (project_counts dict, total_records) of the most recent prior
        successful run for this pipeline, or ({}, 0) if there is none."""
        row = retry_db(
            lambda: self.db.fetchrow(
                f"SELECT records_extracted, metadata->'project_counts' AS project_counts "
                f"FROM {SCHEMA_PIPELINE}.pipeline_runs "
                f"WHERE pipeline_name = $1 AND status = 'success' "
                f"AND completed_at IS NOT NULL AND run_id <> $2 "
                f"ORDER BY completed_at DESC LIMIT 1",
                self._pipeline_name, str(self.run_id)
            ),
            description="fetch previous project counts"
        )
        if not row:
            return {}, 0
        raw = row["project_counts"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        counts = {k: int(v) for k, v in (raw or {}).items()}
        return counts, int(row["records_extracted"] or 0)
```

Ensure `import json` is present at the top of `base_extractor.py` (add if missing).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: PASS (all). If `retry_db` actually invokes the lambda, the `_FakeDB` satisfies it; if `retry_db` needs a real signature, confirm it just calls `fn()` and retries on exception.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/base_extractor.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(base): persist per-project counts to pipeline_runs.metadata + baseline reader"
```

---

### Task 4: 3-round retry loop (testable seam)

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py:591-664` (replace the single retry pass)
- Test: `swift_api_pipeline/tests/test_asset_tasks_resilience.py`

**Interfaces:**
- Produces: `_retry_loop(retry_once, failed_projects: list[str], max_rounds: int = MAX_RETRY_ROUNDS, wait_seconds: int = RETRY_WAIT_SECONDS, sleep=time.sleep) -> list[str]` — calls `retry_once(failed)` (which returns the names still failing that round) up to `max_rounds` times, sleeping `wait_seconds` before each round, stopping early when nothing remains. Returns the final still-failed list.

This isolates the loop control from the threaded extraction so it is unit-testable; the existing per-round body (resume/clean + `ThreadPoolExecutor`) becomes the `retry_once` callable.

- [ ] **Step 1: Write the failing test**

```python
# append to swift_api_pipeline/tests/test_asset_tasks_resilience.py
from extract_asset_tasks import _retry_loop


def test_retry_loop_recovers_in_round_two():
    calls = []
    def retry_once(failed):
        calls.append(list(failed))
        return [] if len(calls) >= 2 else list(failed)  # all recover on round 2
    still = _retry_loop(retry_once, ["TS19"], max_rounds=3, wait_seconds=0, sleep=lambda s: None)
    assert still == []
    assert len(calls) == 2  # stopped early, did not run round 3


def test_retry_loop_exhausts_rounds_when_never_recovers():
    def retry_once(failed):
        return list(failed)  # never recovers
    still = _retry_loop(retry_once, ["TS19"], max_rounds=3, wait_seconds=0, sleep=lambda s: None)
    assert still == ["TS19"]


def test_retry_loop_no_failures_does_nothing():
    def retry_once(failed):
        raise AssertionError("should not be called")
    assert _retry_loop(retry_once, [], wait_seconds=0, sleep=lambda s: None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -k retry_loop -v`
Expected: FAIL with `ImportError: cannot import name '_retry_loop'`

- [ ] **Step 3: Write minimal implementation**

Add at module scope in `extract_asset_tasks.py`:

```python
def _retry_loop(retry_once, failed_projects, max_rounds=MAX_RETRY_ROUNDS,
                wait_seconds=RETRY_WAIT_SECONDS, sleep=time.sleep):
    """Retry failing projects up to max_rounds times, resting between rounds.

    retry_once(failed) performs one retry pass and returns the names still
    failing. Stops early once nothing remains.
    """
    still = list(failed_projects)
    for round_no in range(1, max_rounds + 1):
        if not still:
            break
        logger.warning(
            f"Retry round {round_no}/{max_rounds} for {len(still)} project(s) "
            f"after {wait_seconds}s rest: {still}"
        )
        sleep(wait_seconds)
        still = retry_once(still)
    return still
```

Then refactor the existing block at lines 591-664 so the per-round body (the resume-or-clean prep + `ThreadPoolExecutor` retry that currently runs once) is wrapped in a local `def retry_once(failed_projects):` returning `still_failed`, and replace the single pass with:

```python
        if failed_projects:
            def retry_once(failed_projects):
                # <existing body lines 599-662, operating on the passed-in
                # failed_projects list, returning the still_failed list>
                still_failed = []
                # ... resume/clean prep ...
                # ... ThreadPoolExecutor retry populating project_rows and still_failed ...
                return still_failed

            failed_projects = _retry_loop(retry_once, failed_projects)
```

Keep all existing resume/clean and executor logic byte-for-byte inside `retry_once`; only its inputs (`failed_projects` param) and output (`return still_failed`) change. Remove the now-redundant outer `time.sleep(RETRY_WAIT_SECONDS)` (the loop sleeps before each round).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -k retry_loop -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Verify the module still imports (no syntax break in the refactor)**

Run: `python -c "import sys; sys.path.insert(0,'swift_api_pipeline'); import extract_asset_tasks; print('ok')"`
Expected: prints `ok`

- [ ] **Step 6: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(asset-tasks): retry failing projects up to 3 rounds with rest between rounds"
```

---

### Task 5: Build + return `PipelineOutcome` from the extract; write abnormal note

**Files:**
- Modify: `swift_api_pipeline/extract_asset_tasks.py:666-731` (the tail after the retry loop)
- (Imports) add `from pipeline_notifier import PipelineOutcome` at top of `extract_asset_tasks.py`

**Interfaces:**
- Consumes: `detect_abnormal_counts` (Task 2), `extractor.get_previous_project_counts` (Task 3), `PipelineOutcome` (Task 1).
- Produces: `run_asset_task_pipeline(...)` now returns a `PipelineOutcome` (with `.run_id` set) on every path instead of the bare run-id string.

- [ ] **Step 1: Write the failing test (verifies the new return type contract)**

```python
# append to swift_api_pipeline/tests/test_asset_tasks_resilience.py
import inspect
import extract_asset_tasks as eat


def test_extract_returns_pipeline_outcome_contract():
    # Source-level guard: the success and partial paths must return a PipelineOutcome,
    # not a bare run-id string, so the notifier can classify the run.
    src = inspect.getsource(eat.run_asset_task_pipeline)
    assert "PipelineOutcome(" in src
    assert "return str(extractor.run_id)" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -k pipeline_outcome_contract -v`
Expected: FAIL (current code returns `str(extractor.run_id)`).

- [ ] **Step 3: Write minimal implementation**

At the top of `extract_asset_tasks.py` add: `from pipeline_notifier import PipelineOutcome`.

Replace the tail (current lines 666-731, from `total_records = sum(...)` through the final success log block) with:

```python
        # Recalculate from project_rows — the accumulating counter may be
        # inflated by rows written in round 1 that were deleted before retry.
        total_records = sum(project_rows.values())
        extractor.total_loaded = total_records

        extractor.restore_table_after_load()

        skipped_cleanups = extractor.clear_old_raw_data(projects, project_rows, failed_projects)

        retry_db(
            lambda: extractor.db.execute(
                f'DELETE FROM {SCHEMA_PIPELINE}.extraction_progress WHERE run_id = $1',
                str(extractor.run_id)
            ),
            description="clean extraction_progress"
        )

        # Row-count guard: compare successfully-extracted projects to the previous
        # successful run. Failed projects are excluded (they are already PARTIAL).
        successful_counts = {
            name: count for name, count in project_rows.items()
            if name not in failed_projects
        }
        baseline_counts, _baseline_total = extractor.get_previous_project_counts()
        abnormal_projects = detect_abnormal_counts(successful_counts, baseline_counts)

        # Compose the error_message note so the export guard blocks on partial OR
        # abnormal runs (the guard keys off a non-empty error note).
        notes = []
        if failed_projects:
            succeeded = len(project_rows) - len(failed_projects)
            notes.append(
                f"Partial extraction: {succeeded}/{len(project_rows)} projects "
                f"succeeded. Failed (old data retained): {', '.join(failed_projects)}"
            )
        if abnormal_projects:
            parts = []
            for name in abnormal_projects:
                base = baseline_counts.get(name)
                parts.append(f"{name}: {successful_counts.get(name, 0):,} (prev {base:,})"
                             if base else f"{name}: {successful_counts.get(name, 0):,} (prev n/a)")
            notes.append("Abnormal row counts vs previous run: " + "; ".join(parts))
        error_detail = " | ".join(notes) if notes else None

        # Persist counts of successful projects as next run's baseline. Status stays
        # 'success' so downstream transforms still resolve via WHERE status='success'.
        extractor.complete_pipeline_run(
            "success", total_records, error=error_detail, project_counts=successful_counts
        )

        if error_detail:
            logger.warning(f"\n{'='*60}")
            logger.warning(f"Pipeline DEGRADED: {error_detail}")
            for name, count in sorted(project_rows.items()):
                marker = ""
                if name in failed_projects:
                    marker = " [FAILED - old data retained]"
                elif name in abnormal_projects:
                    marker = " [ABNORMAL ROW COUNT]"
                logger.warning(f"  {name}: {count:,}{marker}")
            logger.warning(f"Total loaded: {total_records:,}")
            logger.warning(f"Run ID: {extractor.run_id}")
            logger.warning(f"{'='*60}\n")
        else:
            logger.info(f"\n{'='*60}")
            logger.info(f"Pipeline completed successfully")
            for name, count in sorted(project_rows.items()):
                logger.info(f"  {name}: {count:,}")
            logger.info(f"Total loaded: {total_records:,}")
            logger.info(f"Run ID: {extractor.run_id}")
            logger.info(f"{'='*60}\n")

        return PipelineOutcome(
            run_id=str(extractor.run_id),
            failed_projects=list(failed_projects),
            abnormal_projects=list(abnormal_projects),
            detail=error_detail or "",
        )
```

Note: this collapses the former separate partial-vs-clean `return` paths into one. If the single-project recovery path (`is_recovery`) has its own earlier `return`, update it to also return a `PipelineOutcome(run_id=str(extractor.run_id))` (clean) for type consistency.

- [ ] **Step 4: Run the full test module**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: PASS (all, including the contract test)

- [ ] **Step 5: Verify import still works**

Run: `python -c "import sys; sys.path.insert(0,'swift_api_pipeline'); import extract_asset_tasks; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add swift_api_pipeline/extract_asset_tasks.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(asset-tasks): return PipelineOutcome + abnormal-count note (export stays blocked on degraded runs)"
```

---

### Task 6: Three-way notification + propagate outcome through main.py

**Files:**
- Modify: `swift_api_pipeline/main.py:389-453` (`run_pipeline_with_notification`)
- Modify: `swift_api_pipeline/main.py:86-118` (`run_asset_tasks_pipeline`), `:121-134` (`run_asset_tasks_extract_pipeline`)
- Test: `swift_api_pipeline/tests/test_asset_tasks_resilience.py`

**Interfaces:**
- Consumes: `PipelineOutcome` (Task 1), `run_asset_task_pipeline` now returns `PipelineOutcome` (Task 5).
- Produces: `run_pipeline_with_notification(...)` sends a red email with `overall_status = outcome.email_status()` when `func()` returns a non-clean `PipelineOutcome` (sent regardless of `email_on_success`), returns `True` (exit 0). Clean outcome or `None`/`True` return -> SUCCESS email as today. Exceptions -> FAILED + raise as today.

- [ ] **Step 1: Write the failing test (monkeypatch DB + email)**

```python
# append to swift_api_pipeline/tests/test_asset_tasks_resilience.py
import importlib


def _patch_notify(monkeypatch):
    main = importlib.import_module("main")
    sent = {}
    monkeypatch.setattr(main, "snapshot_row_counts", lambda tables=None: {})
    def fake_send(**kwargs):
        sent["overall_status"] = kwargs.get("overall_status")
    monkeypatch.setattr(main, "send_pipeline_email", fake_send)
    return main, sent


def test_degraded_outcome_sends_partial_email_and_does_not_raise(monkeypatch):
    main, sent = _patch_notify(monkeypatch)
    from pipeline_notifier import PipelineOutcome
    def func():
        return PipelineOutcome(run_id="r1", failed_projects=["TS19"])
    result = main.run_pipeline_with_notification(func, "asset_tasks", send_email=True,
                                                 email_on_success=True)
    assert result is True
    assert sent["overall_status"] == "PARTIAL FAILURE"


def test_clean_outcome_sends_success(monkeypatch):
    main, sent = _patch_notify(monkeypatch)
    from pipeline_notifier import PipelineOutcome
    def func():
        return PipelineOutcome(run_id="r1")
    main.run_pipeline_with_notification(func, "asset_tasks", send_email=True)
    assert sent["overall_status"] == "SUCCESS"


def test_legacy_none_return_still_success(monkeypatch):
    main, sent = _patch_notify(monkeypatch)
    main.run_pipeline_with_notification(lambda: None, "some_pipeline", send_email=True)
    assert sent["overall_status"] == "SUCCESS"
```

Requires `swift_api_pipeline` on `sys.path` (the module header already inserts the parent dir). Run pytest with rootdir at `swift_api_pipeline` or add `sys.path.insert(0, HERE_parent)` (already done at top of the test file).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -k outcome or legacy -v`
Expected: FAIL — degraded run currently emails `SUCCESS` (the return value is ignored).

- [ ] **Step 3: Implement three-way wrapper**

In `main.py`, ensure `PipelineOutcome` is imported from `pipeline_notifier` (add to the existing import on line 22). Replace the `try:` success arm (lines 400-426) of `run_pipeline_with_notification` with:

```python
        try:
            outcome = func()
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)

            if isinstance(outcome, PipelineOutcome) and not outcome.is_clean():
                overall = outcome.email_status()        # PARTIAL FAILURE | ABNORMAL ROW COUNT
                result = PipelineResult(
                    pipeline_name=name,
                    status=overall,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                    error_message=outcome.detail,
                )
                if send_email:                          # degraded is never routine: ignore email_on_success
                    send_pipeline_email(
                        results=[result],
                        log_output=log_handler.get_log_output(),
                        overall_status=overall,
                        run_label=name,
                        started_at=started_at,
                        ended_at=ended_at,
                        total_duration=duration,
                        recipients=recipients,
                        row_counts_before=row_counts_before,
                        row_counts_after=row_counts_after,
                        row_count_tables=tables,
                    )
                return True   # exit 0 so good projects' transforms/downstream still run

            result = PipelineResult(
                pipeline_name=name,
                status="SUCCESS",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration,
            )
            if send_email and email_on_success:
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="SUCCESS",
                    run_label=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    recipients=recipients,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )
            return True
```

Leave the `except Exception` arm unchanged (FAILED + raise).

- [ ] **Step 4: Propagate the outcome through the asset-tasks wrappers**

In `run_asset_tasks_pipeline` (main.py:103-118), change:

```python
    outcome = run_asset_task_pipeline(project_filter=project_filter)

    run_assets_transform(outcome.run_id)
    run_asset_tasks_transform(outcome.run_id)

    if project_filter:
        from transform import backfill_asset_did, refresh_analytics
        logger.info("Recovery: running backfill_asset_did and analytics refresh...")
        backfill_asset_did()
        refresh_analytics()

    return outcome
```

In `run_asset_tasks_extract_pipeline` (main.py:133-134), change:

```python
    return run_asset_task_pipeline()
```

(Both now hand the `PipelineOutcome` to `run_pipeline_with_notification`, so the extract-only stage email is also honest.)

- [ ] **Step 5: Run the full test module**

Run: `python -m pytest swift_api_pipeline/tests/test_asset_tasks_resilience.py -v`
Expected: PASS (all)

- [ ] **Step 6: Verify both modules import**

Run: `python -c "import sys; sys.path.insert(0,'swift_api_pipeline'); import main, extract_asset_tasks; print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add swift_api_pipeline/main.py swift_api_pipeline/tests/test_asset_tasks_resilience.py
git commit -m "feat(pipeline): SUCCESS email only on clean runs; degraded runs send red email and exit 0"
```

---

## Self-Review

**Spec coverage:**
- "SUCCESS email only on clean run" -> Tasks 1, 5, 6 (outcome carrier + extract returns it + wrapper gates SUCCESS on `is_clean()`). Covered, both full and extract-only stages.
- "Retry failed project after a rest, multiple times" -> Task 4 (3 rounds, 5-min rest). Covered.
- "Failed email if not completed" -> Task 6 (PARTIAL FAILURE red email). Covered (red, names the project; exit 0 by design so good data flows, per approved decision).
- "Abnormal if 0 or less than previous run" -> Task 2 (detect, 0 or >10% drop) + Task 3 (baseline) + Task 5 (per-project AND total via `successful_counts`/baseline; abnormal note + red email). Covered.
- "Export stays blocked on degraded" -> Task 5 writes `error_message` for partial AND abnormal; export guard unchanged. Covered.

**Note on total-vs-per-project:** the guard runs per project; the grand total is implicitly covered because if the sum drops materially it is because one or more projects dropped, which the per-project check catches. `get_previous_project_counts` also returns the prior total if a future explicit total check is wanted; not needed for the stated rules.

**Placeholder scan:** No TBD/TODO; every code step contains full code. The one prose instruction (Task 4 Step 3 "keep existing body inside retry_once") references concrete existing line numbers 599-662 and preserves them verbatim.

**Type consistency:** `PipelineOutcome` fields/methods (`run_id`, `failed_projects`, `abnormal_projects`, `detail`, `is_clean()`, `email_status()`) are used identically in Tasks 1, 5, 6. `detect_abnormal_counts(current, baseline, drop_pct)` signature consistent Tasks 2/5. `complete_pipeline_run(..., project_counts=)` and `get_previous_project_counts()` consistent Tasks 3/5. `email_status()` returns exactly `"PARTIAL FAILURE"`/`"ABNORMAL ROW COUNT"`/`"SUCCESS"`, matched in the wrapper.

**Verification caveats to confirm during execution:**
- Confirm the extractor base class name (`BaseExtractor`) and that `retry_db(fn, description=...)` simply calls `fn()` (Task 3 test assumes so).
- Confirm `pipeline_runs.metadata` is jsonb (it is written a dict at `start_pipeline_run`, line 80) so `|| $5::jsonb` merges cleanly.
- Confirm `run_asset_task_pipeline`'s single-project recovery path returns a `PipelineOutcome` too (Task 5 note).
