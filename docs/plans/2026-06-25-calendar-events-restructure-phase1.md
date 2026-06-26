# Calendar Events Restructure — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the brittle positional calendar-leave parser with a validated deterministic fast-path + cached AI structured-extraction, land it in a conformed `stg_calendar_events` table with an `event_kind` discriminator and soft-delete, built beside the live table and cut over by repointing the HR view.

**Architecture:** Keep extract/raw/incremental-sync. Split the transform into focused, unit-testable modules (`calendar_parse.py` pure functions, `calendar_ai_extract.py` Haiku extraction, `calendar_parse_cache.py` resolver + persisted cache). A new `stg_calendar_events` table is populated from the existing raw JSONB, diffed against the old `stg_calendar_leave`, then cut over via a gated migration.

**Tech Stack:** Python 3.14, asyncpg via `db.py`, `anthropic` SDK (Claude Haiku `claude-haiku-4-5-20251001`), Supabase/Postgres. Tests are plain `test_*.py` files run with `venv/Scripts/python test_x.py` (assert-based, matches existing `test_summary.py`).

## Global Constraints

- **Schemas we may touch only:** `data_raw`, `data_staging`, `analytics`, `pipeline`, `agent`, `reference`. Never `public`/`auth`/etc.
- **Every DB object via numbered migration** in `migrations/NNN_*.sql`; next number is **124**. New schema objects need an `agent.schema_metadata` row and a PK on anything upserted. Migrations are applied via the Supabase MCP `apply_migration`.
- **Convert UTC → America/New_York on display** (`AT TIME ZONE 'America/New_York'`). Store UTC.
- **No em-dash in any copy/comments/strings** (`" - "` reads as AI). Use period/comma/colon/parens. The leave-summary separator `" - "` is data, not prose, and is fine.
- **Build beside, never TRUNCATE the live table.** `stg_calendar_leave` and `analytics.v_calendar_leave` keep serving the HR app until the gated cutover (Task 14).
- **AI model pin:** `claude-haiku-4-5-20251001`. API key from env `CLAUDE_API_KEY`.
- **Parsed-event shape** (the dict every parse function and the cache return), used verbatim across tasks:
  ```python
  {
    "event_kind": str,          # "leave" | "holiday" | "birthday" | "training" | "other"
    "leave_type": str | None,   # raw code token, e.g. "VL", "UT/SL"
    "team": str | None,         # raw team token, e.g. "CG1", "Zeta"
    "person": str | None,       # cleaned person name (no weekday, no note)
    "person_note": str | None,
    "rest_day_of_week": str | None,  # "Mon".."Sun" when a weekday sat in the person slot
    "confidence": float,        # 0.0 .. 1.0
    "parse_source": str,        # "deterministic" | "ai"
    "needs_review": bool,
  }
  ```
- **Confidence gate:** `CONFIDENCE_GATE = 0.8`. A deterministic parse with `confidence < CONFIDENCE_GATE` is not trusted and is routed to AI.

---

### Task 1: Migration 124 — conformed table + parse cache (non-destructive)

Creates the new objects beside the live ones. No rename, no view change here (those are Task 14).

**Files:**
- Create: `migrations/124_calendar_events_restructure.sql`
- Test: `migrations/_verify_124.sql` (verification queries, run via MCP `execute_sql`)

**Interfaces:**
- Produces: table `data_staging.stg_calendar_events`, table `agent.calendar_summary_parse`. Column names per the shape above plus identity/date/quality columns.

- [ ] **Step 1: Write the migration SQL**

```sql
-- migrations/124_calendar_events_restructure.sql
-- Phase 1 of the calendar pipeline restructure. Creates the conformed
-- stg_calendar_events table and the agent.calendar_summary_parse cache,
-- beside the live stg_calendar_leave (which keeps serving until cutover).

CREATE TABLE IF NOT EXISTS data_staging.stg_calendar_events (
    event_id              text PRIMARY KEY,
    ical_uid              text,
    summary_raw           text,
    event_kind            text NOT NULL DEFAULT 'other',
    leave_type            text,
    leave_type_normalized text,
    team                  text,
    team_normalized       text,
    person                text,
    person_note           text,
    rest_day_of_week      text,
    start_date            date,
    end_date              date,
    days                  integer,
    is_all_day            boolean,
    creator_email         text,
    event_created         timestamptz,
    event_updated         timestamptz,
    parse_source          text,
    parse_confidence      real,
    needs_review          boolean NOT NULL DEFAULT false,
    is_deleted            boolean NOT NULL DEFAULT false,
    deleted_at            timestamptz,
    run_id                text,
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    parsed_at             timestamptz,
    CONSTRAINT chk_event_kind
        CHECK (event_kind IN ('leave','holiday','birthday','training','other')),
    CONSTRAINT chk_leave_norm_only_leave
        CHECK (leave_type_normalized IS NULL OR event_kind = 'leave'),
    CONSTRAINT chk_restday_only_leave
        CHECK (rest_day_of_week IS NULL OR event_kind = 'leave'),
    CONSTRAINT chk_restday_value
        CHECK (rest_day_of_week IS NULL OR
               rest_day_of_week IN ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'))
);

CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_kind
    ON data_staging.stg_calendar_events (event_kind);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_start_date
    ON data_staging.stg_calendar_events (start_date);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_person
    ON data_staging.stg_calendar_events (person);
CREATE INDEX IF NOT EXISTS idx_stg_calendar_events_needs_review
    ON data_staging.stg_calendar_events (needs_review) WHERE needs_review;

CREATE TABLE IF NOT EXISTS agent.calendar_summary_parse (
    summary_key       text PRIMARY KEY,   -- whitespace-collapsed summary_raw
    event_kind        text NOT NULL,
    leave_type        text,
    team              text,
    person            text,
    person_note       text,
    rest_day_of_week  text,
    confidence        real,
    parse_source      text NOT NULL,      -- 'deterministic' | 'ai'
    needs_review      boolean NOT NULL DEFAULT false,
    model             text,
    prompt_version    text,
    extracted_at      timestamptz NOT NULL DEFAULT now()
);

INSERT INTO agent.schema_metadata (schema_name, object_name, description)
VALUES
  ('data_staging','stg_calendar_events',
   'Conformed calendar events (leave/holiday/birthday/training/other) with soft-delete. Phase 1 restructure of stg_calendar_leave.'),
  ('agent','calendar_summary_parse',
   'Persisted parse cache keyed on calendar summary string; makes extraction deterministic across runs.')
ON CONFLICT DO NOTHING;
```

- [ ] **Step 2: Apply via MCP and verify objects exist**

Apply `124_calendar_events_restructure.sql` with the Supabase MCP `apply_migration` (project `voqfjfngdpcvevbkikud`). Then run:

```sql
SELECT to_regclass('data_staging.stg_calendar_events') AS t,
       to_regclass('agent.calendar_summary_parse')    AS c;
```
Expected: both non-null.

- [ ] **Step 3: Verify CHECK constraints reject bad rows**

Run (expect ERROR on each):
```sql
INSERT INTO data_staging.stg_calendar_events (event_id, event_kind) VALUES ('t1','bogus');
INSERT INTO data_staging.stg_calendar_events (event_id, event_kind, leave_type_normalized)
VALUES ('t2','holiday','VL');
```
Expected: first fails `chk_event_kind`; second fails `chk_leave_norm_only_leave`. Clean up any test rows: `DELETE FROM data_staging.stg_calendar_events WHERE event_id IN ('t1','t2');`

- [ ] **Step 4: Commit**

```bash
git add migrations/124_calendar_events_restructure.sql migrations/_verify_124.sql
git commit -m "feat(calendar): migration 124 - conformed stg_calendar_events + parse cache"
```

---

### Task 2: `calendar_parse.py` — separator normalization + clean 3-part split

The deterministic core, starting with the defect-1 fix (teams ending in a digit).

**Files:**
- Create: `swift_api_pipeline/calendar_parse.py`
- Test: `swift_api_pipeline/test_calendar_parse.py`

**Interfaces:**
- Produces: `normalize_separators(summary: str) -> str`; constants `KNOWN_LEAVE_CODES: set[str]`, `KNOWN_TEAMS: set[str]`.

- [ ] **Step 1: Write the failing test**

```python
# swift_api_pipeline/test_calendar_parse.py
"""Unit tests for the deterministic calendar parser. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py
"""
from calendar_parse import normalize_separators


def test_normalize_digit_team_dash():
    # Defect 1: "CG1- Angelica" must become "CG1 - Angelica" so the split works.
    assert normalize_separators("VL - CG1- Angelica") == "VL - CG1 - Angelica"


def test_normalize_leading_dash():
    assert normalize_separators("SDL -CG1 - Tads") == "SDL - CG1 - Tads"


def test_normalize_letter_dash_unchanged_when_already_spaced():
    assert normalize_separators("VL - Zeta - Luis") == "VL - Zeta - Luis"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: ImportError / NameError (`calendar_parse` or `normalize_separators` not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_parse.py
"""Deterministic, pure-function parser for calendar event summaries.

No DB, no network. The summary convention is "LeaveType - Team - Person (note)"
but the source is a free-text shared calendar, so anything that does not parse
to a validated 3-part shape is returned with low confidence for AI fallback.
"""
import re

KNOWN_LEAVE_CODES = {
    "RD", "RDOT", "RDO", "VL", "SL", "EL", "SDL", "UT", "BL", "ML",
    "PL", "SPL", "STL", "BRL", "LR", "WW", "LAC", "HD", "PH", "LWOP",
}

KNOWN_TEAMS = {
    "CG1", "CG2", "CG3", "ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA",
    "QPI", "CRTV", "CRTVS", "ADMIN AND OPS", "ADMIN & OPS", "ACCTG", "ACCOUNTING",
    "R&D", "DA", "T&A", "TNA", "HR", "SWIFT", "MKTG", "MARKETING",
    "TS ADMIN", "TS OPS", "PHI DS", "PHIDS", "PHIDSM", "DSM", "PHI HR",
    "TRAINEE", "SD", "AI", "AIE", "T&D",
}


def normalize_separators(summary: str) -> str:
    """Fix inconsistent dash spacing so split(' - ') works, including teams
    that end in a digit (e.g. 'CG1- Angelica')."""
    s = re.sub(r" -(\S)", r" - \1", summary)        # " -X" -> " - X"
    s = re.sub(r"(\w)- (?=\S)", r"\1 - ", s)        # "X- "  -> "X - " (\w includes digits)
    return s
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: no assertion error (exit 0).

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_parse.py swift_api_pipeline/test_calendar_parse.py
git commit -m "feat(calendar): separator normalization handles digit-prefixed teams (defect 1)"
```

---

### Task 3: `calendar_parse.py` — weekday + note helpers

Pure helpers used by the parse function: weekday detection (defect 4) and note extraction (defect 5).

**Files:**
- Modify: `swift_api_pipeline/calendar_parse.py`
- Test: `swift_api_pipeline/test_calendar_parse.py`

**Interfaces:**
- Produces: `canonical_weekday(token: str) -> str | None` (returns `"Mon".."Sun"` or None); `split_note(person_raw: str) -> tuple[str | None, str | None]` (returns `(person, note)`).

- [ ] **Step 1: Write the failing tests**

```python
# append to test_calendar_parse.py
from calendar_parse import canonical_weekday, split_note


def test_canonical_weekday_variants():
    assert canonical_weekday("Tue") == "Tue"
    assert canonical_weekday("Tues") == "Tue"
    assert canonical_weekday("Tuesday") == "Tue"
    assert canonical_weekday("WEDNESDAY") == "Wed"
    assert canonical_weekday("Thurs") == "Thu"
    assert canonical_weekday("Merj") is None


def test_split_note_parenthetical():
    assert split_note("Chesca (3pm onwards)") == ("Chesca", "3pm onwards")


def test_split_note_unparenthesized_trailing():
    # Defect 5: "Mik - In by 12PM" -> person "Mik", note "In by 12PM".
    assert split_note("Mik - In by 12PM") == ("Mik", "In by 12PM")


def test_split_note_plain_name():
    assert split_note("Luis") == ("Luis", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: ImportError for `canonical_weekday` / `split_note`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to calendar_parse.py
_WEEKDAYS = {
    "MON": "Mon", "MONDAY": "Mon",
    "TUE": "Tue", "TUES": "Tue", "TUESDAY": "Tue",
    "WED": "Wed", "WEDS": "Wed", "WEDNESDAY": "Wed",
    "THU": "Thu", "THUR": "Thu", "THURS": "Thu", "THURSDAY": "Thu",
    "FRI": "Fri", "FRIDAY": "Fri",
    "SAT": "Sat", "SATURDAY": "Sat",
    "SUN": "Sun", "SUNDAY": "Sun",
}
_NOTE_PAREN_RE = re.compile(r"\s*\(([^)]+)\)\s*$")


def canonical_weekday(token: str) -> str | None:
    """Map a day-of-week token to canonical 'Mon'..'Sun', else None."""
    if not token:
        return None
    return _WEEKDAYS.get(token.strip().upper())


def split_note(person_raw: str) -> tuple[str | None, str | None]:
    """Separate a trailing note from a person name. Handles a parenthetical
    '(note)' and an unparenthesized ' - note' tail."""
    if not person_raw:
        return None, None
    m = _NOTE_PAREN_RE.search(person_raw)
    if m:
        return _NOTE_PAREN_RE.sub("", person_raw).strip(), m.group(1).strip()
    if " - " in person_raw:
        head, tail = person_raw.split(" - ", 1)
        return head.strip(), tail.strip()
    return person_raw.strip(), None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_parse.py swift_api_pipeline/test_calendar_parse.py
git commit -m "feat(calendar): weekday and note helpers (defects 4,5)"
```

---

### Task 4: `calendar_parse.py` — event_kind classification

Classifies a summary into the coarse `event_kind` (defects 2,3 routing).

**Files:**
- Modify: `swift_api_pipeline/calendar_parse.py`
- Test: `swift_api_pipeline/test_calendar_parse.py`

**Interfaces:**
- Produces: `classify_kind(summary: str, leave_type: str | None) -> str` returning one of `leave|holiday|birthday|training|other`.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_calendar_parse.py
from calendar_parse import classify_kind


def test_classify_holiday():
    assert classify_kind("PH Holiday: Labor Day", "PH") == "holiday"
    assert classify_kind("Christmas Holiday (Company-Wide)", None) == "holiday"


def test_classify_birthday():
    assert classify_kind("Ced's Birthday!", None) == "birthday"


def test_classify_training():
    assert classify_kind("AT&T COP Refresher Course", None) == "training"
    assert classify_kind("Swift Projects Training Walkthrough", None) == "training"


def test_classify_leave_from_known_code():
    assert classify_kind("VL - Zeta - Luis", "VL") == "leave"


def test_classify_other_blank():
    assert classify_kind("", None) == "other"
    assert classify_kind("230701\tRoel Longcop Annual Performance Evaluation", None) == "other"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: ImportError for `classify_kind`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to calendar_parse.py
def classify_kind(summary: str, leave_type: str | None) -> str:
    """Coarse event-kind classification. Order matters: holiday/birthday/training
    are recognized by summary text; a known leave code wins otherwise."""
    s = (summary or "").lower()
    if not summary or not summary.strip():
        return "other"
    if "holiday" in s or summary.startswith(("PH:", "PH Holiday:", "US:")):
        return "holiday"
    if "birthday" in s or "bday" in s or "b-day" in s or "anniversary" in s:
        return "birthday"
    if any(k in s for k in ("refresher", "training", "walkthrough", "course", "webinar")):
        return "training"
    code = (leave_type or "").upper().replace("/", "")
    if code in KNOWN_LEAVE_CODES or (leave_type and "/" in leave_type):
        return "leave"
    return "other"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_parse.py swift_api_pipeline/test_calendar_parse.py
git commit -m "feat(calendar): event_kind classification"
```

---

### Task 5: `calendar_parse.py` — `deterministic_parse` with confidence gate

Assembles the helpers into the full deterministic parse that emits the shape + a calibrated confidence. This is the crux: it must be self-doubting (defects 1-5 must not produce a confident wrong row).

**Files:**
- Modify: `swift_api_pipeline/calendar_parse.py`
- Test: `swift_api_pipeline/test_calendar_parse.py`

**Interfaces:**
- Consumes: `normalize_separators`, `canonical_weekday`, `split_note`, `classify_kind`, `KNOWN_LEAVE_CODES`, `KNOWN_TEAMS`.
- Produces: `deterministic_parse(summary: str) -> dict` (the shape; `parse_source="deterministic"`, `confidence` in 0..1, `needs_review` False here); `CONFIDENCE_GATE = 0.8`.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_calendar_parse.py
from calendar_parse import deterministic_parse, CONFIDENCE_GATE


def test_parse_clean_three_part_high_confidence():
    r = deterministic_parse("VL - Zeta - Luis")
    assert r["event_kind"] == "leave"
    assert r["leave_type"] == "VL"
    assert r["team"] == "Zeta"
    assert r["person"] == "Luis"
    assert r["rest_day_of_week"] is None
    assert r["confidence"] >= CONFIDENCE_GATE


def test_parse_digit_team_now_splits_clean():
    r = deterministic_parse("VL - CG1- Angelica")
    assert r["leave_type"] == "VL"
    assert r["team"] == "CG1"
    assert r["person"] == "Angelica"
    assert r["confidence"] >= CONFIDENCE_GATE


def test_parse_rest_day_weekday_to_field_not_person():
    r = deterministic_parse("RD - Alpha - Fri")
    assert r["event_kind"] == "leave"
    assert r["leave_type"] == "RD"
    assert r["team"] == "Alpha"
    assert r["person"] is None
    assert r["rest_day_of_week"] == "Fri"
    assert r["confidence"] >= CONFIDENCE_GATE


def test_parse_underscore_low_confidence():
    # Defect 3: "VL_CRTV_Nicolai" must NOT confidently land in leave_type.
    r = deterministic_parse("VL_CRTV_Nicolai")
    assert r["confidence"] < CONFIDENCE_GATE


def test_parse_no_separator_noise_low_or_classified():
    r = deterministic_parse("Ced's Birthday!")
    assert r["event_kind"] == "birthday"
    # not leave, and leave_type must be None for a non-leave kind
    assert r["leave_type"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: ImportError for `deterministic_parse` / `CONFIDENCE_GATE`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to calendar_parse.py
CONFIDENCE_GATE = 0.8

_BASE_SHAPE_KEYS = (
    "event_kind", "leave_type", "team", "person", "person_note",
    "rest_day_of_week", "confidence", "parse_source", "needs_review",
)


def _shape(**kw) -> dict:
    base = {k: None for k in _BASE_SHAPE_KEYS}
    base.update({"parse_source": "deterministic", "needs_review": False,
                 "confidence": 0.0, "event_kind": "other"})
    base.update(kw)
    return base


def deterministic_parse(summary: str) -> dict:
    """Parse a summary into the conformed shape with a calibrated confidence.
    Confidence is high only when the result is a positively-validated leave
    row; anything ambiguous returns < CONFIDENCE_GATE for AI fallback."""
    summary = (summary or "").strip()
    kind = classify_kind(summary, None)

    # Non-leave kinds: leave-specific fields stay null; confidence high when the
    # text signal is unambiguous (holiday/birthday/training keywords).
    if kind in ("holiday", "birthday", "training"):
        return _shape(event_kind=kind, confidence=0.9)
    if not summary:
        return _shape(event_kind="other", confidence=0.9)

    norm = normalize_separators(summary)
    parts = [p.strip() for p in norm.split(" - ")] if " - " in norm else None
    if not parts or len(parts) < 3:
        # 1-2 parts, underscores, or unsplittable: do not trust it.
        return _shape(event_kind="other", confidence=0.3)

    leave_type = parts[0]
    team = parts[1]
    person_raw = " - ".join(parts[2:])

    code_ok = leave_type.upper().replace("/", "") in KNOWN_LEAVE_CODES or "/" in leave_type
    team_ok = team.upper() in KNOWN_TEAMS

    weekday = canonical_weekday(person_raw)
    if weekday:
        person, note = None, None
    else:
        person, note = split_note(person_raw)

    # Confidence: both code and team validated -> trust; one unknown -> route to AI.
    if code_ok and team_ok:
        confidence = 0.95
    elif code_ok or team_ok:
        confidence = 0.6
    else:
        confidence = 0.3

    return _shape(
        event_kind="leave" if code_ok else "other",
        leave_type=leave_type if code_ok else None,
        team=team,
        person=person,
        person_note=note,
        rest_day_of_week=weekday if code_ok else None,
        confidence=confidence,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_parse.py swift_api_pipeline/test_calendar_parse.py
git commit -m "feat(calendar): deterministic_parse with self-doubting confidence gate"
```

---

### Task 6: `calendar_ai_extract.py` — Haiku structured extraction

AI fallback for low-confidence summaries. Returns the same shape, schema-guided, with a hard fallback to `needs_review` on any failure (never crash).

**Files:**
- Create: `swift_api_pipeline/calendar_ai_extract.py`
- Test: `swift_api_pipeline/test_calendar_ai_extract.py`

**Interfaces:**
- Consumes: `KNOWN_LEAVE_CODES`, `KNOWN_TEAMS` from `calendar_parse`.
- Produces: `extract_with_ai(summary: str, client=None) -> dict` (shape with `parse_source="ai"`); `PROMPT_VERSION: str`; `_parse_ai_json(text: str) -> dict` (pure, testable without network).

- [ ] **Step 1: Write the failing test**

```python
# swift_api_pipeline/test_calendar_ai_extract.py
"""Unit tests for AI extraction JSON handling (no network). Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_ai_extract.py
"""
from calendar_ai_extract import _parse_ai_json


def test_parse_ai_json_with_fences_and_prose():
    text = 'Sure!\n```json\n{"event_kind":"leave","leave_type":"VL","team":"CRTV",' \
           '"person":"Nicolai","person_note":null,"rest_day_of_week":null}\n```'
    out = _parse_ai_json(text)
    assert out["leave_type"] == "VL"
    assert out["team"] == "CRTV"
    assert out["person"] == "Nicolai"


def test_parse_ai_json_garbage_returns_none():
    assert _parse_ai_json("no json here") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_ai_extract.py`
Expected: ImportError for `_parse_ai_json`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_ai_extract.py
"""Claude Haiku structured extraction for calendar summaries the deterministic
parser could not confidently handle. Returns the conformed shape; any failure
degrades to needs_review rather than raising."""
import os
import re
import json

import anthropic

from calendar_parse import KNOWN_LEAVE_CODES, KNOWN_TEAMS

MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = "2026-06-25.1"


def _build_prompt(summary: str) -> str:
    codes = ", ".join(sorted(KNOWN_LEAVE_CODES))
    teams = ", ".join(sorted(KNOWN_TEAMS))
    return (
        "You extract structured fields from one entry on a company's shared "
        "leave/work calendar. The loose convention is 'LeaveType - Team - Person "
        "(note)' but entries are inconsistent.\n\n"
        f"Known leave codes: {codes}\n"
        f"Known teams: {teams}\n\n"
        "Rules:\n"
        "- event_kind is one of: leave, holiday, birthday, training, other.\n"
        "- leave_type is a known code (or compound like 'UT/SL'); null if not leave.\n"
        "- If a weekday (Mon..Sun) sits where a person should be, set rest_day_of_week "
        "(Mon/Tue/Wed/Thu/Fri/Sat/Sun) and person null.\n"
        "- Strip trailing notes from person into person_note.\n"
        "- Birthdays, performance evaluations, trainings are NOT leave.\n\n"
        f"Entry: {json.dumps(summary)}\n\n"
        "Return ONLY a JSON object with keys: event_kind, leave_type, team, person, "
        "person_note, rest_day_of_week."
    )


def _parse_ai_json(text: str) -> dict | None:
    """Strip fences/prose and load the first {...} object. None on failure."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, json.JSONDecodeError):
        return None


def _shape_from_ai(d: dict) -> dict:
    return {
        "event_kind": d.get("event_kind") or "other",
        "leave_type": d.get("leave_type"),
        "team": d.get("team"),
        "person": d.get("person"),
        "person_note": d.get("person_note"),
        "rest_day_of_week": d.get("rest_day_of_week"),
        "confidence": 0.75,
        "parse_source": "ai",
        "needs_review": False,
    }


def _needs_review_shape() -> dict:
    return {
        "event_kind": "other", "leave_type": None, "team": None, "person": None,
        "person_note": None, "rest_day_of_week": None, "confidence": 0.0,
        "parse_source": "ai", "needs_review": True,
    }


def extract_with_ai(summary: str, client=None) -> dict:
    """Call Haiku for one summary. Degrades to needs_review on any error."""
    try:
        client = client or anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=512,
            messages=[{"role": "user", "content": _build_prompt(summary)}],
        )
        parsed = _parse_ai_json(resp.content[0].text)
        return _shape_from_ai(parsed) if parsed else _needs_review_shape()
    except Exception:
        return _needs_review_shape()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_ai_extract.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_ai_extract.py swift_api_pipeline/test_calendar_ai_extract.py
git commit -m "feat(calendar): Haiku structured extraction with needs_review fallback"
```

---

### Task 7: `calendar_parse_cache.py` — resolver + persisted cache

The single entry point the transform uses: `resolve(summary)`. Cache hit returns instantly; miss runs deterministic; below-gate runs AI; result is written to `agent.calendar_summary_parse`. This is what makes incremental and full-refresh identical.

**Files:**
- Create: `swift_api_pipeline/calendar_parse_cache.py`
- Test: `swift_api_pipeline/test_calendar_parse_cache.py`

**Interfaces:**
- Consumes: `deterministic_parse`, `CONFIDENCE_GATE` from `calendar_parse`; `extract_with_ai`, `PROMPT_VERSION`, `MODEL` from `calendar_ai_extract`.
- Produces: `summary_key(summary: str) -> str`; `resolve(db, summary: str, ai_fn=extract_with_ai) -> dict` (shape). `ai_fn` is injectable for tests.

- [ ] **Step 1: Write the failing tests**

```python
# swift_api_pipeline/test_calendar_parse_cache.py
"""Unit tests for the parse-cache resolver using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_parse_cache.py
"""
from calendar_parse_cache import summary_key, resolve


class FakeDB:
    """Minimal db stub: in-memory agent.calendar_summary_parse."""
    def __init__(self, rows=None):
        self.rows = rows or {}     # summary_key -> record dict
        self.writes = 0

    def fetchrow(self, query, *args):
        return self.rows.get(args[0])

    def execute(self, query, *args):
        self.writes += 1
        self.rows[args[0]] = {"summary_key": args[0]}
        return "INSERT 0 1"


def test_summary_key_collapses_whitespace():
    assert summary_key("  VL  -   Zeta -  Luis ") == "VL - Zeta - Luis"


def test_resolve_clean_uses_deterministic_and_writes_cache():
    db = FakeDB()
    calls = {"n": 0}

    def ai_fn(summary, client=None):
        calls["n"] += 1
        return {"event_kind": "other"}

    r = resolve(db, "VL - Zeta - Luis", ai_fn=ai_fn)
    assert r["parse_source"] == "deterministic"
    assert r["leave_type"] == "VL"
    assert calls["n"] == 0          # AI not called for a clean parse
    assert db.writes == 1           # result cached


def test_resolve_low_confidence_calls_ai():
    db = FakeDB()
    calls = {"n": 0}

    def ai_fn(summary, client=None):
        calls["n"] += 1
        return {"event_kind": "leave", "leave_type": "VL", "team": "CRTV",
                "person": "Nicolai", "person_note": None, "rest_day_of_week": None,
                "confidence": 0.75, "parse_source": "ai", "needs_review": False}

    r = resolve(db, "VL_CRTV_Nicolai", ai_fn=ai_fn)
    assert calls["n"] == 1
    assert r["parse_source"] == "ai"
    assert r["team"] == "CRTV"


def test_resolve_cache_hit_skips_both():
    cached = {"summary_key": "VL - Zeta - Luis", "event_kind": "leave",
              "leave_type": "VL", "team": "Zeta", "person": "Luis",
              "person_note": None, "rest_day_of_week": None, "confidence": 0.95,
              "parse_source": "deterministic", "needs_review": False}
    db = FakeDB(rows={"VL - Zeta - Luis": cached})

    def ai_fn(summary, client=None):
        raise AssertionError("AI must not be called on cache hit")

    r = resolve(db, "VL - Zeta - Luis", ai_fn=ai_fn)
    assert r["leave_type"] == "VL"
    assert db.writes == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse_cache.py`
Expected: ImportError for `summary_key` / `resolve`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_parse_cache.py
"""Resolve a calendar summary to the conformed parse shape, caching the result
in agent.calendar_summary_parse so incremental and full-refresh runs produce
identical output and repeat strings cost nothing."""
import re

from calendar_parse import deterministic_parse, CONFIDENCE_GATE
from calendar_ai_extract import extract_with_ai, PROMPT_VERSION, MODEL

_CACHE_COLS = ("event_kind", "leave_type", "team", "person", "person_note",
               "rest_day_of_week", "confidence", "parse_source", "needs_review")


def summary_key(summary: str) -> str:
    """Whitespace-collapsed key. Do not over-normalize (would merge distinct strings)."""
    return re.sub(r"\s+", " ", (summary or "").strip())


def _row_to_shape(row) -> dict:
    return {k: row[k] for k in _CACHE_COLS}


def _write_cache(db, key: str, shape: dict):
    db.execute(
        "INSERT INTO agent.calendar_summary_parse "
        "(summary_key, event_kind, leave_type, team, person, person_note, "
        " rest_day_of_week, confidence, parse_source, needs_review, model, prompt_version) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
        "ON CONFLICT (summary_key) DO UPDATE SET "
        "  event_kind=EXCLUDED.event_kind, leave_type=EXCLUDED.leave_type, "
        "  team=EXCLUDED.team, person=EXCLUDED.person, person_note=EXCLUDED.person_note, "
        "  rest_day_of_week=EXCLUDED.rest_day_of_week, confidence=EXCLUDED.confidence, "
        "  parse_source=EXCLUDED.parse_source, needs_review=EXCLUDED.needs_review, "
        "  model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version, extracted_at=now()",
        key, shape["event_kind"], shape["leave_type"], shape["team"], shape["person"],
        shape["person_note"], shape["rest_day_of_week"], shape["confidence"],
        shape["parse_source"], shape["needs_review"],
        MODEL if shape["parse_source"] == "ai" else None,
        PROMPT_VERSION if shape["parse_source"] == "ai" else None,
    )


def resolve(db, summary: str, ai_fn=extract_with_ai) -> dict:
    """Cache -> deterministic -> AI (if below gate). Always returns the shape."""
    key = summary_key(summary)
    row = db.fetchrow("SELECT * FROM agent.calendar_summary_parse WHERE summary_key = $1", key)
    if row is not None:
        return _row_to_shape(row)

    shape = deterministic_parse(summary)
    if shape["confidence"] < CONFIDENCE_GATE:
        shape = ai_fn(summary)
    _write_cache(db, key, shape)
    return shape
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_parse_cache.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_parse_cache.py swift_api_pipeline/test_calendar_parse_cache.py
git commit -m "feat(calendar): parse-cache resolver (cache -> deterministic -> AI)"
```

---

### Task 8: `calendar_events_transform.py` — date/days + row builder

Pure builder that turns a raw Google event dict + a resolved shape into a `stg_calendar_events` row dict. Reuses the existing date/days logic from `extract_calendar_leave.parse_event`.

**Files:**
- Create: `swift_api_pipeline/calendar_events_transform.py`
- Test: `swift_api_pipeline/test_calendar_events_transform.py`

**Interfaces:**
- Consumes: `resolve` (passed in), the shape.
- Produces: `build_row(ev: dict, shape: dict, run_id: str) -> dict` with keys matching `stg_calendar_events` columns; `event_dates(ev: dict) -> tuple[date, date, int, bool]`.

- [ ] **Step 1: Write the failing tests**

```python
# swift_api_pipeline/test_calendar_events_transform.py
"""Unit tests for the staging row builder. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_events_transform.py
"""
from datetime import date
from calendar_events_transform import event_dates, build_row


def _allday(start, end):
    return {"id": "e1", "summary": "VL - Zeta - Luis",
            "start": {"date": start}, "end": {"date": end},
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-02T00:00:00Z",
            "creator": {"email": "a@ontel.co"}}


def test_event_dates_allday_inclusive():
    s, e, days, allday = event_dates(_allday("2026-03-02", "2026-03-05"))
    assert s == date(2026, 3, 2)
    assert e == date(2026, 3, 4)     # exclusive end -> inclusive
    assert days == 3
    assert allday is True


def test_build_row_maps_shape_and_kind():
    shape = {"event_kind": "leave", "leave_type": "VL", "team": "Zeta",
             "person": "Luis", "person_note": None, "rest_day_of_week": None,
             "confidence": 0.95, "parse_source": "deterministic", "needs_review": False}
    row = build_row(_allday("2026-03-02", "2026-03-03"), shape, "run-1")
    assert row["event_id"] == "e1"
    assert row["event_kind"] == "leave"
    assert row["leave_type"] == "VL"
    assert row["person"] == "Luis"
    assert row["run_id"] == "run-1"
    assert row["is_deleted"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_transform.py`
Expected: ImportError for `event_dates` / `build_row`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_events_transform.py
"""Pure builders: raw Google event + resolved parse shape -> stg_calendar_events row."""
from datetime import datetime, date, timedelta


def event_dates(ev: dict):
    """Return (start_date, end_date_inclusive, days, is_all_day)."""
    start_obj, end_obj = ev.get("start", {}), ev.get("end", {})
    is_all_day = "date" in start_obj
    if is_all_day:
        start_date = date.fromisoformat(start_obj["date"])
        end_date = date.fromisoformat(end_obj["date"])      # exclusive
        days = (end_date - start_date).days
        if days > 0:
            end_date = end_date - timedelta(days=1)          # make inclusive
        days = max(days, 1)
    else:
        start_dt = datetime.fromisoformat(start_obj["dateTime"])
        end_dt = datetime.fromisoformat(end_obj["dateTime"])
        start_date, end_date = start_dt.date(), end_dt.date()
        days = max((end_date - start_date).days, 1)
    return start_date, end_date, days, is_all_day


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def build_row(ev: dict, shape: dict, run_id: str) -> dict:
    summary = (ev.get("summary") or "").strip() or None
    start_date, end_date, days, is_all_day = event_dates(ev)
    return {
        "event_id": ev.get("id", ""),
        "ical_uid": ev.get("iCalUID"),
        "summary_raw": summary,
        "event_kind": shape["event_kind"],
        "leave_type": shape["leave_type"],
        "leave_type_normalized": None,        # populated in Phase 2 (ref_leave_code)
        "team": shape["team"],
        "team_normalized": None,              # populated in Phase 2
        "person": shape["person"],
        "person_note": shape["person_note"],
        "rest_day_of_week": shape["rest_day_of_week"],
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "is_all_day": is_all_day,
        "creator_email": (ev.get("creator") or {}).get("email"),
        "event_created": _ts(ev.get("created")),
        "event_updated": _ts(ev.get("updated")),
        "parse_source": shape["parse_source"],
        "parse_confidence": shape["confidence"],
        "needs_review": shape["needs_review"],
        "is_deleted": False,
        "run_id": run_id,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_transform.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_events_transform.py swift_api_pipeline/test_calendar_events_transform.py
git commit -m "feat(calendar): pure staging row builder"
```

---

### Task 9: `calendar_events_load.py` — upsert + tombstone-on-cancelled

DB load: upsert built rows into `stg_calendar_events`, and soft-delete (tombstone) events that come back `status="cancelled"` (defect 6). Integration-style; tested with a fake db that records calls.

**Files:**
- Create: `swift_api_pipeline/calendar_events_load.py`
- Test: `swift_api_pipeline/test_calendar_events_load.py`

**Interfaces:**
- Consumes: `build_row`; `resolve(db, summary)`.
- Produces: `load_staging(db, run_id, events, resolve_fn) -> dict` returning counts `{"upserted": int, "tombstoned": int, "skipped": int}`.

- [ ] **Step 1: Write the failing tests**

```python
# swift_api_pipeline/test_calendar_events_load.py
"""Tests for staging load + tombstone using a fake db. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_events_load.py
"""
from calendar_events_load import load_staging


class FakeDB:
    def __init__(self):
        self.upserts = []
        self.tombstones = []

    def execute(self, query, *args):
        if "is_deleted = true" in query:
            self.tombstones.append(args)
        return "OK"

    def executemany(self, query, args):
        self.upserts.extend(args)


def _shape():
    return {"event_kind": "leave", "leave_type": "VL", "team": "Zeta",
            "person": "Luis", "person_note": None, "rest_day_of_week": None,
            "confidence": 0.95, "parse_source": "deterministic", "needs_review": False}


def test_load_upserts_active_and_tombstones_cancelled():
    db = FakeDB()
    events = [
        {"id": "e1", "summary": "VL - Zeta - Luis", "status": "confirmed",
         "start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"},
         "created": "2026-01-01T00:00:00Z", "updated": "2026-01-02T00:00:00Z",
         "creator": {"email": "a@ontel.co"}},
        {"id": "e2", "status": "cancelled", "start": {}, "end": {}},
    ]
    counts = load_staging(db, "run-1", events, resolve_fn=lambda d, s: _shape())
    assert counts["upserted"] == 1
    assert counts["tombstoned"] == 1
    assert db.tombstones[0][0] == "e2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_load.py`
Expected: ImportError for `load_staging`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_events_load.py
"""Load conformed rows into data_staging.stg_calendar_events. Cancelled events
are soft-deleted (tombstoned), not skipped, so deletions propagate."""
import logging
from datetime import datetime, timezone

from config import SCHEMA_STAGING, retry_db
from calendar_events_transform import build_row

logger = logging.getLogger("calendar_leave")

LOAD_BATCH_SIZE = 500

_UPSERT_COLS = [
    "event_id", "ical_uid", "summary_raw", "event_kind", "leave_type",
    "leave_type_normalized", "team", "team_normalized", "person", "person_note",
    "rest_day_of_week", "start_date", "end_date", "days", "is_all_day",
    "creator_email", "event_created", "event_updated", "parse_source",
    "parse_confidence", "needs_review", "is_deleted", "run_id",
]


def _upsert_sql() -> str:
    cols = ", ".join(_UPSERT_COLS)
    ph = ", ".join(f"${i+1}" for i in range(len(_UPSERT_COLS)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLS if c != "event_id")
    return (
        f"INSERT INTO {SCHEMA_STAGING}.stg_calendar_events ({cols}, parsed_at, loaded_at) "
        f"VALUES ({ph}, now(), now()) "
        f"ON CONFLICT (event_id) DO UPDATE SET {updates}, "
        f"  is_deleted = false, deleted_at = NULL, parsed_at = now(), loaded_at = now()"
    )


def _tombstone(db, event_id: str):
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_STAGING}.stg_calendar_events "
            f"SET is_deleted = true, deleted_at = $2 WHERE event_id = $1",
            event_id, datetime.now(timezone.utc),
        ),
        description=f"tombstone {event_id}",
    )


def load_staging(db, run_id: str, events: list, resolve_fn) -> dict:
    upserted = tombstoned = skipped = 0
    rows = []
    for ev in events:
        if ev.get("status") == "cancelled":
            _tombstone(db, ev.get("id", ""))
            tombstoned += 1
            continue
        try:
            shape = resolve_fn(db, ev.get("summary") or "")
            rows.append(build_row(ev, shape, run_id))
        except Exception as e:
            skipped += 1
            logger.warning(f"  Skipped event {ev.get('id','?')}: {e}")

    sql = _upsert_sql()
    for i in range(0, len(rows), LOAD_BATCH_SIZE):
        batch = rows[i:i + LOAD_BATCH_SIZE]
        tuples = [tuple(r[c] for c in _UPSERT_COLS) for r in batch]
        retry_db(lambda t=tuples: db.executemany(sql, t),
                 description=f"upsert stg_calendar_events batch {i // LOAD_BATCH_SIZE + 1}")
        upserted += len(batch)

    logger.info(f"  Upserted {upserted}, tombstoned {tombstoned}, skipped {skipped}")
    return {"upserted": upserted, "tombstoned": tombstoned, "skipped": skipped}
```

Note: the FakeDB test calls `executemany`/`execute` directly; `retry_db` simply invokes its lambda, so the test's fake methods are exercised. Confirm `retry_db` is importable from `config`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_load.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_events_load.py swift_api_pipeline/test_calendar_events_load.py
git commit -m "feat(calendar): staging upsert + tombstone-on-cancelled (defect 6)"
```

---

### Task 10: `calendar_events_fetch.py` — watermark-from-raw + 12-month forward cap

Fetch helpers: incremental watermark sourced from raw at full precision with a safety overlap (§5.6), and a forward extract cap of `today + 12 months` (§7).

**Files:**
- Create: `swift_api_pipeline/calendar_events_fetch.py`
- Test: `swift_api_pipeline/test_calendar_events_fetch.py`

**Interfaces:**
- Produces: `forward_time_max(today: date) -> str` (RFC3339, today+12 months); `watermark_from_raw(db) -> str | None` (max event updated minus overlap, RFC3339). Uses `SCHEMA_RAW`.

- [ ] **Step 1: Write the failing tests**

```python
# swift_api_pipeline/test_calendar_events_fetch.py
"""Tests for fetch-window helpers. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_events_fetch.py
"""
from datetime import date, datetime, timezone
from calendar_events_fetch import forward_time_max, watermark_from_raw


def test_forward_time_max_is_12_months_out():
    assert forward_time_max(date(2026, 6, 25)).startswith("2027-06-25")


class FakeDB:
    def __init__(self, ts):
        self._ts = ts

    def fetchval(self, query, *args, **kw):
        return self._ts


def test_watermark_applies_overlap_and_full_precision():
    ts = datetime(2026, 6, 25, 10, 30, 15, 500000, tzinfo=timezone.utc)
    out = watermark_from_raw(FakeDB(ts))
    # 60s overlap subtracted -> 10:29:15, millisecond precision retained
    assert out.startswith("2026-06-25T10:29:15")


def test_watermark_none_when_empty():
    assert watermark_from_raw(FakeDB(None)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_fetch.py`
Expected: ImportError for `forward_time_max` / `watermark_from_raw`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_events_fetch.py
"""Fetch-window helpers: forward 12-month cap and a raw-sourced incremental
watermark with a safety overlap (re-processing is idempotent; skipping loses data)."""
from datetime import date, datetime, timezone, timedelta

from config import SCHEMA_RAW

WATERMARK_OVERLAP_SECONDS = 60


def forward_time_max(today: date) -> str:
    """RFC3339 timestamp 12 months ahead of `today` (forward extract cap)."""
    y, m = today.year, today.month + 12
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(today.day, 28)        # avoid month-length edge cases
    return datetime(y, m, day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def watermark_from_raw(db) -> str | None:
    """Max event 'updated' seen in raw, minus an overlap. Sourced from raw (not
    staging) so staging can be rebuilt without disturbing incremental sync."""
    ts = db.fetchval(
        f"SELECT max((data->>'updated')::timestamptz) FROM {SCHEMA_RAW}.raw_calendar_events"
    )
    if ts is None:
        return None
    ts = ts.astimezone(timezone.utc) - timedelta(seconds=WATERMARK_OVERLAP_SECONDS)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
```

Note: this reads `data_raw.raw_calendar_events`, which exists only after the Task 14 rename. Until cutover, the running pipeline still uses `raw_calendar_leave`; this helper is exercised by the backfill (Task 12) against whichever raw table name is current. If running before cutover, temporarily point the query at `raw_calendar_leave` (the backfill harness passes the table name).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_fetch.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_events_fetch.py swift_api_pipeline/test_calendar_events_fetch.py
git commit -m "feat(calendar): forward 12-month cap + raw-sourced watermark with overlap"
```

---

### Task 11: `_backfill_calendar_events.py` — re-clean all raw into the new table

One-off backfill that reads existing raw JSONB, resolves every event through the cache (populating it), and upserts into `stg_calendar_events`. Reuses Tasks 7-9. Underscore-prefixed per repo convention for throwaway scripts.

**Files:**
- Create: `swift_api_pipeline/_backfill_calendar_events.py`

**Interfaces:**
- Consumes: `resolve`, `load_staging`. Reads from a raw table (default `raw_calendar_leave` pre-cutover).

- [ ] **Step 1: Write the backfill script**

```python
# swift_api_pipeline/_backfill_calendar_events.py
"""One-off: re-clean all raw calendar events into data_staging.stg_calendar_events,
populating agent.calendar_summary_parse. Reads the latest raw payload per event_id.

Usage:
    cd swift_api_pipeline && venv/Scripts/python _backfill_calendar_events.py
"""
import uuid

from config import SCHEMA_RAW, get_db, close_db, setup_logging, get_logger
from calendar_parse_cache import resolve
from calendar_events_load import load_staging

setup_logging()
logger = get_logger("calendar_leave")

RAW_TABLE = "raw_calendar_leave"   # pre-cutover name


def main():
    db = get_db()
    run_id = f"backfill-{uuid.uuid4()}"
    rows = db.fetch(
        f"SELECT DISTINCT ON (event_id) event_id, data "
        f"FROM {SCHEMA_RAW}.{RAW_TABLE} "
        f"ORDER BY event_id, loaded_at DESC"
    )
    events = [r["data"] for r in (rows or [])]
    logger.info(f"Backfilling {len(events)} distinct events from {RAW_TABLE}")
    counts = load_staging(db, run_id, events, resolve_fn=resolve)
    logger.info(f"Backfill done: {counts}")
    close_db()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the backfill (WARP on, against the new table)**

Run: `cd swift_api_pipeline && venv/Scripts/python _backfill_calendar_events.py`
Expected: log lines `Backfilling N distinct events` then `Backfill done: {'upserted': ..., 'tombstoned': 0, 'skipped': ...}`. (If WARP/auth conflicts block local DB, run the equivalent via the GHA workflow or a MCP-driven batch.)

- [ ] **Step 3: Verify row counts landed**

Run via MCP `execute_sql`:
```sql
SELECT count(*) total,
       count(*) FILTER (WHERE event_kind='leave') leave,
       count(*) FILTER (WHERE needs_review) needs_review,
       count(*) FILTER (WHERE parse_source='ai') ai
FROM data_staging.stg_calendar_events;
```
Expected: `total` close to the old 15,129 (minus far-future rows if the cap applied during backfill); `needs_review` small and reviewable.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/_backfill_calendar_events.py
git commit -m "feat(calendar): backfill script re-cleans raw into stg_calendar_events"
```

---

### Task 12: `_diff_calendar_events.py` — old-vs-new QA gate

The validation gate (§5.7). Produces a diff report: counts by kind, every row where person/team/leave_type changed vs `stg_calendar_leave`, and explicit defect-oracle checks. Writes to `out/`.

**Files:**
- Create: `swift_api_pipeline/_diff_calendar_events.py`

**Interfaces:**
- Reads `stg_calendar_leave` (old) and `stg_calendar_events` (new); writes `out/calendar_events_diff.md`.

- [ ] **Step 1: Write the diff script**

```python
# swift_api_pipeline/_diff_calendar_events.py
"""Compare old stg_calendar_leave vs new stg_calendar_events as the cutover gate.
Writes out/calendar_events_diff.md. Run:
    cd swift_api_pipeline && venv/Scripts/python _diff_calendar_events.py
"""
from config import get_db, close_db, setup_logging, get_logger

setup_logging()
logger = get_logger("calendar_leave")

# event_id -> sample summaries proving each defect class is fixed.
DEFECT_ORACLE = {
    "digit_team": "VL - CG1- Angelica",      # team should be CG1, person Angelica
    "underscore": "VL_CRTV_Nicolai",         # team CRTV, person Nicolai
    "rest_day":  "RD - Alpha - Fri",         # rest_day_of_week Fri, person NULL
}


def _section(title, rows):
    out = [f"## {title}", ""]
    if not rows:
        out.append("_none_")
    for r in rows:
        out.append(f"- {dict(r)}")
    out.append("")
    return "\n".join(out)


def main():
    db = get_db()
    parts = ["# Calendar events diff (old vs new)", ""]

    kinds = db.fetch(
        "SELECT event_kind, count(*) n FROM data_staging.stg_calendar_events "
        "GROUP BY event_kind ORDER BY n DESC")
    parts.append(_section("New counts by event_kind", kinds))

    changed = db.fetch(
        "SELECT o.event_id, o.summary, "
        "  o.person old_person, n.person new_person, "
        "  o.team old_team, n.team new_team, "
        "  o.leave_type old_lt, n.leave_type new_lt "
        "FROM data_staging.stg_calendar_leave o "
        "JOIN data_staging.stg_calendar_events n USING (event_id) "
        "WHERE o.person IS DISTINCT FROM n.person "
        "   OR o.team IS DISTINCT FROM n.team "
        "   OR o.leave_type IS DISTINCT FROM n.leave_type "
        "ORDER BY o.summary")
    parts.append(_section(f"Changed rows ({len(changed or [])})", changed))

    oracle_rows = db.fetch(
        "SELECT summary_raw, leave_type, team, person, rest_day_of_week, event_kind "
        "FROM data_staging.stg_calendar_events "
        "WHERE summary_raw = ANY($1)",
        list(DEFECT_ORACLE.values()))
    parts.append(_section("Defect-oracle rows (must be correct)", oracle_rows))

    missing = db.fetch(
        "SELECT o.event_id, o.summary FROM data_staging.stg_calendar_leave o "
        "LEFT JOIN data_staging.stg_calendar_events n USING (event_id) "
        "WHERE n.event_id IS NULL")
    parts.append(_section(f"Rows in OLD but missing in NEW ({len(missing or [])})", missing))

    report = "\n".join(parts)
    with open("out/calendar_events_diff.md", "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("Wrote out/calendar_events_diff.md")
    close_db()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the diff**

Run: `cd swift_api_pipeline && venv/Scripts/python _diff_calendar_events.py`
Expected: `out/calendar_events_diff.md` written.

- [ ] **Step 3: Manually review the gate (human checkpoint)**

Open `out/calendar_events_diff.md`. Confirm: (a) defect-oracle rows are all correct (CG1/Angelica split, CRTV/Nicolai, RD/Fri with null person); (b) "missing in NEW" is empty or explained; (c) eyeball 100% of changed rows and the `needs_review` set. Do NOT proceed to cutover until clean.

- [ ] **Step 4: Commit**

```bash
git add swift_api_pipeline/_diff_calendar_events.py
git commit -m "feat(calendar): old-vs-new diff QA gate with defect oracle"
```

---

### Task 13: Reconciliation against live Calendar

Soft-delete staging rows whose events are no longer present in a live full Calendar listing (§5.5). Added to the pipeline core and runnable standalone.

**Files:**
- Create: `swift_api_pipeline/calendar_events_reconcile.py`
- Test: `swift_api_pipeline/test_calendar_events_reconcile.py`

**Interfaces:**
- Produces: `reconcile(db, live_event_ids: set[str]) -> int` (count soft-deleted). `live_event_ids` is the set currently returned by a full Calendar listing; the caller supplies it (keeps this unit testable without the API).

- [ ] **Step 1: Write the failing test**

```python
# swift_api_pipeline/test_calendar_events_reconcile.py
"""Test reconciliation soft-deletes absent events. Run:
    cd swift_api_pipeline && venv/Scripts/python test_calendar_events_reconcile.py
"""
from calendar_events_reconcile import reconcile


class FakeDB:
    def __init__(self, staged):
        self.staged = staged           # list of event_id currently active
        self.soft_deleted = []

    def fetch(self, query, *args):
        return [{"event_id": e} for e in self.staged]

    def execute(self, query, *args):
        self.soft_deleted.append(args[0])   # array of ids
        return "OK"


def test_reconcile_soft_deletes_absent():
    db = FakeDB(staged=["e1", "e2", "e3"])
    n = reconcile(db, live_event_ids={"e1", "e3"})
    assert n == 1
    assert "e2" in db.soft_deleted[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_reconcile.py`
Expected: ImportError for `reconcile`.

- [ ] **Step 3: Write minimal implementation**

```python
# swift_api_pipeline/calendar_events_reconcile.py
"""Reconcile staging against a live full Calendar listing: any active staged
event not present live is soft-deleted. Catches deletions that incremental
updatedMin sync can miss."""
import logging
from datetime import datetime, timezone

from config import SCHEMA_STAGING, retry_db

logger = logging.getLogger("calendar_leave")


def reconcile(db, live_event_ids: set) -> int:
    active = retry_db(
        lambda: db.fetch(
            f"SELECT event_id FROM {SCHEMA_STAGING}.stg_calendar_events "
            f"WHERE NOT is_deleted"),
        description="fetch active staged events")
    stale = [r["event_id"] for r in (active or []) if r["event_id"] not in live_event_ids]
    if not stale:
        return 0
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_STAGING}.stg_calendar_events "
            f"SET is_deleted = true, deleted_at = $2 WHERE event_id = ANY($1)",
            stale, datetime.now(timezone.utc)),
        description=f"reconcile soft-delete {len(stale)} events")
    logger.info(f"  Reconciliation soft-deleted {len(stale)} events")
    return len(stale)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd swift_api_pipeline && venv/Scripts/python test_calendar_events_reconcile.py`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/calendar_events_reconcile.py swift_api_pipeline/test_calendar_events_reconcile.py
git commit -m "feat(calendar): live-Calendar reconciliation soft-delete"
```

---

### Task 14: Cutover — new orchestrator, rename raw, repoint views (gated)

Wire the new modules into a runnable pipeline, then atomically rename raw and repoint the HR view. **Gated:** only run after Task 12's diff is clean.

**Files:**
- Create: `swift_api_pipeline/extract_calendar_events.py` (orchestrator using fetch/load/reconcile)
- Create: `migrations/125_calendar_events_cutover.sql`
- Modify: `.github/workflows/pipeline-calendar-leave.yml` (run the new script)

**Interfaces:**
- Consumes: `forward_time_max`, `watermark_from_raw`, `load_staging`, `reconcile`, `resolve`, plus existing `authenticate_calendar`, pipeline-run tracking, notifier.

- [ ] **Step 1: Write the orchestrator**

```python
# swift_api_pipeline/extract_calendar_events.py
"""Calendar Events pipeline (Phase 1 transform). Extract -> raw -> conformed
stg_calendar_events via cached parse, with soft-delete + reconciliation.

Usage:
    python extract_calendar_events.py                 # incremental
    python extract_calendar_events.py --full-refresh  # re-resolve all raw
"""
import os
import uuid
import argparse
from datetime import datetime, timezone

from config import SCHEMA_RAW, get_db, close_db, retry_db, setup_logging, get_logger
from calendar_client import authenticate_calendar
from calendar_events_fetch import forward_time_max, watermark_from_raw
from calendar_events_load import load_staging
from calendar_events_reconcile import reconcile
from calendar_parse_cache import resolve

setup_logging()
logger = get_logger("calendar_leave")

CALENDAR_ID = "c_9b404e3738157b5b83e066ba4e0d2dcddbcb2b9bf60b4027620c1d939636c778@group.calendar.google.com"
TIME_MIN = "2024-01-01T00:00:00Z"


def _fetch(service, updated_min, time_max):
    params = {"calendarId": CALENDAR_ID, "maxResults": 2500, "singleEvents": True,
              "orderBy": "startTime", "timeMin": TIME_MIN, "timeMax": time_max}
    if updated_min:
        params["updatedMin"] = updated_min
    events, token = [], None
    while True:
        if token:
            params["pageToken"] = token
        res = service.events().list(**params).execute()
        events.extend(res.get("items", []))
        token = res.get("nextPageToken")
        if not token:
            break
    return events


def _load_raw(db, run_id, events):
    for i in range(0, len(events), 500):
        batch = events[i:i + 500]
        tuples = [(run_id, ev.get("id", ""), ev) for ev in batch]
        retry_db(lambda t=tuples: db.executemany(
            f"INSERT INTO {SCHEMA_RAW}.raw_calendar_events (run_id, event_id, data) "
            f"VALUES ($1,$2,$3)", t), description=f"insert raw batch {i//500+1}")


def main(full_refresh: bool = False):
    db = get_db()
    run_id = str(uuid.uuid4())
    service = authenticate_calendar()
    time_max = forward_time_max(datetime.now(timezone.utc).date())
    updated_min = None if full_refresh else watermark_from_raw(db)

    events = _fetch(service, updated_min, time_max)
    logger.info(f"Fetched {len(events)} events (full_refresh={full_refresh})")
    if events:
        _load_raw(db, run_id, events)
        counts = load_staging(db, run_id, events, resolve_fn=resolve)
        logger.info(f"Load: {counts}")

    # Reconcile on full listings only (updated_min is None == full window).
    if updated_min is None:
        live_ids = {ev.get("id") for ev in events}
        reconcile(db, live_ids)

    close_db()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-refresh", action="store_true")
    main(full_refresh=ap.parse_args().full_refresh)
```

- [ ] **Step 2: Write the cutover migration**

```sql
-- migrations/125_calendar_events_cutover.sql
-- GATED: apply only after the old-vs-new diff (Task 12) is clean.
-- Renames raw to the generic name and repoints the HR view contract.

ALTER TABLE data_raw.raw_calendar_leave RENAME TO raw_calendar_events;

-- Preserve the HR app's read contract: same view name, new conformed base,
-- leave-only, excluding soft-deleted rows.
CREATE OR REPLACE VIEW analytics.v_calendar_leave AS
SELECT event_id, summary_raw AS summary, leave_type, leave_type_normalized,
       team, team_normalized, person, person_note, rest_day_of_week,
       start_date, end_date, days, is_all_day, creator_email,
       event_created, event_updated, run_id, loaded_at
FROM data_staging.stg_calendar_events
WHERE event_kind = 'leave' AND NOT is_deleted;

-- Daily-exploded view, same scope.
CREATE OR REPLACE VIEW analytics.v_calendar_leave_daily AS
SELECT e.*, gs::date AS leave_date
FROM analytics.v_calendar_leave e
CROSS JOIN LATERAL generate_series(e.start_date, e.end_date, interval '1 day') gs;

UPDATE agent.schema_metadata
SET description = description || ' [renamed from raw_calendar_leave 2026-06-25]'
WHERE schema_name = 'data_raw' AND object_name = 'raw_calendar_events';
```

- [ ] **Step 3: Apply cutover migration and verify the view contract**

Apply `125_calendar_events_cutover.sql` via MCP between the 6am/6pm reads. Then verify the HR app's columns still resolve:
```sql
SELECT count(*), min(start_date), max(start_date) FROM analytics.v_calendar_leave;
```
Expected: count matches the leave subset from the diff; no error on the columns the HR app selects.

- [ ] **Step 4: Point the GHA workflow at the new script**

In `.github/workflows/pipeline-calendar-leave.yml`, change the run step from `extract_calendar_leave.py` to `extract_calendar_events.py`. Verify:

Run: `grep -n "extract_calendar" .github/workflows/pipeline-calendar-leave.yml`
Expected: shows `extract_calendar_events.py`, no remaining `extract_calendar_leave.py`.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/extract_calendar_events.py migrations/125_calendar_events_cutover.sql .github/workflows/pipeline-calendar-leave.yml
git commit -m "feat(calendar): cutover - orchestrator, raw rename, view repoint (migration 125)"
```

- [ ] **Step 6: Post-cutover smoke + rollback insurance**

Trigger one workflow run (or `workflow_dispatch`). Confirm it writes to `raw_calendar_events`, upserts `stg_calendar_events`, and the HR app reads normally. Keep `stg_calendar_leave` frozen ~1 week as rollback; drop it in a later migration once stable.

---

## Self-Review

**Spec coverage:**
- §5.1 rename → Task 14 (migration 125). HR view contract preserved → Task 14 Step 2. ✓
- §5.2 conformed table + event_kind + CHECKs → Task 1. ✓
- §5.3 parser rebuild (normalize, gate, weekday, note, classify, deterministic) → Tasks 2-5; AI extraction → Task 6. ✓
- §5.4 persisted cache → Tasks 1 (table) + 7 (resolver). ✓
- §5.5 soft-delete + reconciliation → Task 9 (tombstone) + Task 13 (reconcile). ✓
- §5.6 watermark from raw, full precision, overlap → Task 10. ✓
- §5.7 backfill/diff/cutover → Tasks 11, 12, 14. ✓
- §5.8 operational signals (needs_review / ai counts) → Task 11 Step 3 query (per-run logging is emitted by `load_staging`). ✓
- §7 retention: 12-month forward cap → Task 10; keep history → no deletion task (by design). ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output. ✓

**Type consistency:** The parse shape dict (9 keys) is identical across `deterministic_parse`, `extract_with_ai`, the cache `_CACHE_COLS`, and `build_row`. `resolve(db, summary, ai_fn)` signature matches its callers in `load_staging` (`resolve_fn(db, summary)`) and the orchestrator. `_UPSERT_COLS` matches the table columns from migration 124. ✓

**Known follow-ups (Phase 2, not this plan):** `leave_type_normalized` / `team_normalized` stay NULL in Phase 1 (Task 8 notes this); ref_employees identity, ref_holidays, ref_leave_code, and the unified serving views are Phase 2.
