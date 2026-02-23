#!/usr/bin/env python3
"""
Calendar Leave Pipeline -- Google Calendar to Supabase

Extracts leave/RD/weekend-work events from the shared Google Calendar,
stores raw JSONB in data_raw, and parses into data_staging.

Summary format: "Type of leave - Group - Person (optional note)"
Examples:
    "RD - Admin and Ops - Merj"
    "VL - Zeta - Luis"
    "UT/SL - ACCTG - Chesca (3pm onwards)"

Modes:
    Default (incremental): Only fetches events updated since last run.
                           Upserts into staging (new events added, changed events updated).
    --full-refresh:        Re-fetches all events and truncates+reloads staging.

Usage:
    python extract_calendar_leave.py                # incremental
    python extract_calendar_leave.py --full-refresh  # full refresh
"""

import os
import re
import json
import uuid
import argparse
from datetime import datetime, timezone, date, timedelta

import anthropic
from dotenv import load_dotenv

from config import (
    SCHEMA_RAW, SCHEMA_STAGING, SCHEMA_PIPELINE,
    get_logger, get_db, close_db, retry_db, setup_logging,
)
from calendar_client import authenticate_calendar
from pipeline_notifier import (
    PipelineResult, PIPELINE_TABLES, capture_logs,
    send_pipeline_email, snapshot_row_counts,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "local-ai-agent", "backend", ".env"))

logger = get_logger("calendar_leave")

CALENDAR_ID = "c_9b404e3738157b5b83e066ba4e0d2dcddbcb2b9bf60b4027620c1d939636c778@group.calendar.google.com"
TIME_MIN = "2024-01-01T00:00:00Z"
LOAD_BATCH_SIZE = 500


# ------------------------------------------------------------------
# Pipeline run tracking
# ------------------------------------------------------------------

def start_pipeline_run(db, run_id: str, metadata: dict = None):
    retry_db(
        lambda: db.execute(
            f"INSERT INTO {SCHEMA_PIPELINE}.pipeline_runs "
            f"(run_id, pipeline_name, status, started_at, metadata) "
            f"VALUES ($1, $2, $3, $4, $5)",
            run_id, "calendar_leave", "running",
            datetime.now(timezone.utc), metadata,
        ),
        description="insert pipeline_runs",
    )


def complete_pipeline_run(db, run_id: str, status: str, records: int = None, error: str = None):
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_PIPELINE}.pipeline_runs "
            f"SET status = $1, completed_at = $2, records_extracted = $3, error_message = $4 "
            f"WHERE run_id = $5",
            status, datetime.now(timezone.utc), records, error, run_id,
        ),
        description="update pipeline_runs",
    )


# ------------------------------------------------------------------
# Extract: Google Calendar -> raw table
# ------------------------------------------------------------------

def get_last_updated(db) -> str | None:
    """Get the latest event_updated timestamp from staging for incremental sync."""
    row = db.fetchrow(
        f"SELECT MAX(event_updated) as last_updated "
        f"FROM {SCHEMA_STAGING}.stg_calendar_leave"
    )
    if row and row["last_updated"]:
        # Add 1 second to avoid re-fetching the exact same event
        ts = row["last_updated"] + timedelta(seconds=1)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return None


def fetch_events(service, updated_min: str = None) -> list:
    """Fetch events from the leave calendar (paginated).

    Args:
        updated_min: If set, only fetch events updated after this ISO timestamp.
                     If None, fetches all events from TIME_MIN onward.
    """
    all_events = []
    page_token = None

    params = {
        "calendarId": CALENDAR_ID,
        "maxResults": 2500,
        "singleEvents": True,
        "orderBy": "startTime",
        "timeMin": TIME_MIN,
    }
    if updated_min:
        params["updatedMin"] = updated_min
        logger.info(f"  Incremental mode: fetching events updated after {updated_min}")

    while True:
        if page_token:
            params["pageToken"] = page_token

        result = service.events().list(**params).execute()

        events = result.get("items", [])
        all_events.extend(events)
        logger.info(f"  Fetched page: {len(events)} events (total so far: {len(all_events)})")

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return all_events


def load_raw_events(db, run_id: str, events: list):
    """Insert raw events as JSONB into data_raw.raw_calendar_leave."""
    for i in range(0, len(events), LOAD_BATCH_SIZE):
        batch = events[i:i + LOAD_BATCH_SIZE]
        tuples = [(run_id, ev.get("id", ""), ev) for ev in batch]
        retry_db(
            lambda t=tuples: db.executemany(
                f"INSERT INTO {SCHEMA_RAW}.raw_calendar_leave (run_id, event_id, data) "
                f"VALUES ($1, $2, $3)",
                t,
            ),
            description=f"insert raw_calendar_leave batch {i // LOAD_BATCH_SIZE + 1}",
        )
    logger.info(f"  Loaded {len(events)} raw events")


# ------------------------------------------------------------------
# Transform: raw JSONB -> staging
# ------------------------------------------------------------------

# Regex to extract parenthetical note from person name
_NOTE_RE = re.compile(r"\s*\(([^)]+)\)\s*$")

# Known leave type codes (uppercase)
_LEAVE_CODES = {
    "RD", "RDOT", "RDO", "VL", "SL", "EL", "SDL", "UT", "BL", "ML",
    "PL", "SPL", "STL", "BRL", "LR", "WW", "LAC", "HD", "PH",
}

# 2-part entries where the second part is a leave description (person is first)
_LEAVE_DESCRIPTIONS = {"Weekend Work", "Birthday Leave", "Live Review"}

# Known team names for detecting swapped team/person or team/leave_type fields
_KNOWN_TEAMS = {
    "CG1", "CG2", "CG3",
    "Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta",
    "QPI", "CRTV", "CRTVS", "Admin and Ops", "Admin & Ops",
    "Acctg", "ACCTG", "Accounting", "R&D", "DA", "T&A", "TNA",
    "HR", "Swift", "MKTG", "Marketing",
    "TS Admin", "PHI DS", "PHIDS", "PHIDSM", "DSM",
    "PHI HR", "Trainee", "SD", "AI", "AIE", "T&D",
}
_KNOWN_TEAMS_UPPER = {t.upper() for t in _KNOWN_TEAMS}


def _normalize_separators(summary: str) -> str:
    """Fix inconsistent dash spacing so split(' - ') works.

    Handles: 'RD- Alpha', 'SDL -CG1', 'Admin and Ops -Merj'
    """
    # " -X" (space-dash-letter, no trailing space) → " - X"
    s = re.sub(r" -([A-Za-z])", r" - \1", summary)
    # "X- " (letter-dash-space) → "X - "
    s = re.sub(r"([A-Za-z])- ", r"\1 - ", s)
    return s


def parse_summary(summary: str) -> dict:
    """Parse 'LeaveType - Team - Person (note)' into components.

    Returns dict with keys: leave_type, team, person, person_note.

    Handles variants:
      - Standard 3-part: "VL - Zeta - Luis"
      - Holidays: "PH: Independence Day", "PH Holiday: Labor Day"
      - 2-part person-leave: "Steph - Weekend Work"
      - 2-part leave-person (no team): "SL - Francis"
      - Missing spaces: "RD- Alpha - Mon", "SDL -CG1 - Tads"
      - No spaces at all: "SDL-CG1-Tads"
    """
    empty = {"leave_type": None, "team": None, "person": None, "person_note": None}
    if not summary:
        return empty

    # Holiday patterns — no team/person
    if summary.startswith(("PH:", "PH Holiday:", "US:")):
        return {"leave_type": "PH", "team": None, "person": None, "person_note": summary}

    # Broader holiday keywords (catches "Christmas Holiday (Company-Wide)" etc.)
    if "holiday" in summary.lower():
        return {"leave_type": "PH", "team": None, "person": None, "person_note": summary}

    # Normalize dash spacing
    normalized = _normalize_separators(summary)

    # Split on " - "
    if " - " in normalized:
        parts = [p.strip() for p in normalized.split(" - ")]
    elif "-" in normalized:
        # No " - " at all — try bare dash (e.g. "SDL-CG1-Tads")
        parts = [p.strip() for p in normalized.split("-")]
    else:
        # Single token, no dashes
        return {"leave_type": summary.strip(), "team": None, "person": None, "person_note": None}

    if len(parts) >= 3:
        leave_type = parts[0]
        team = parts[1]
        person_raw = " - ".join(parts[2:])

        # Detect swapped leave_type <-> team: e.g. "QPI - SL - Paolo"
        if (leave_type.upper() in _KNOWN_TEAMS_UPPER
                and team.upper().replace("/", "") in _LEAVE_CODES):
            leave_type, team = team, leave_type

        # Detect swapped team <-> person: e.g. "SDL - Euge - CG1"
        person_clean = re.sub(r'\s*\([^)]*\)\s*$', '', person_raw).strip()
        if (person_clean.upper() in _KNOWN_TEAMS_UPPER
                and team.upper() not in _KNOWN_TEAMS_UPPER):
            team, person_raw = person_raw, team
    elif len(parts) == 2:
        first, second = parts[0], parts[1]
        first_upper = first.upper().replace("/", "").strip()

        if second in _LEAVE_DESCRIPTIONS:
            # "Steph - Weekend Work" → person=Steph, leave_type=WW
            leave_type = second
            team = None
            person_raw = first
        elif first_upper in _LEAVE_CODES or "/" in first:
            # "SL - Francis" → leave_type=SL, person=Francis (no team)
            leave_type = first
            team = None
            person_raw = second
        else:
            # Unknown 2-part — store as leave_type + team
            leave_type = first
            team = second
            person_raw = None
    else:
        leave_type = parts[0]
        team = None
        person_raw = None

    # Extract parenthetical note from person name
    person_note = None
    person = person_raw
    if person_raw:
        m = _NOTE_RE.search(person_raw)
        if m:
            person_note = m.group(1).strip()
            person = _NOTE_RE.sub("", person_raw).strip()

    return {
        "leave_type": leave_type.strip() if leave_type else None,
        "team": team.strip() if team else None,
        "person": person.strip() if person else None,
        "person_note": person_note,
    }


def parse_event(ev: dict, run_id: str) -> dict:
    """Parse a single raw calendar event into a staging row."""
    summary = (ev.get("summary") or "").strip()
    parsed = parse_summary(summary)

    # Dates -- all-day events use 'date', timed events use 'dateTime'
    start_obj = ev.get("start", {})
    end_obj = ev.get("end", {})

    is_all_day = "date" in start_obj
    if is_all_day:
        start_date = date.fromisoformat(start_obj["date"])
        # Google Calendar all-day end dates are exclusive
        end_date = date.fromisoformat(end_obj["date"])
    else:
        # Timed event -- extract just the date portion
        start_dt = datetime.fromisoformat(start_obj["dateTime"])
        end_dt = datetime.fromisoformat(end_obj["dateTime"])
        start_date = start_dt.date()
        end_date = end_dt.date()
        # For timed events that start and end on the same day, end_date = start_date
        # but we add 1 day so days calculation works consistently
        if end_date == start_date:
            end_date = start_date

    # For all-day events, days = end - start (end is exclusive)
    # For timed events on same day, days = 1
    if is_all_day:
        days = (end_date - start_date).days
        # Adjust end_date to be inclusive for storage (subtract 1 day)
        if days > 0:
            end_date = end_date - timedelta(days=1)
        if days < 1:
            days = 1
    else:
        days = max((end_date - start_date).days, 1)

    # Timestamps
    event_created = None
    if ev.get("created"):
        event_created = datetime.fromisoformat(ev["created"].replace("Z", "+00:00"))
    event_updated = None
    if ev.get("updated"):
        event_updated = datetime.fromisoformat(ev["updated"].replace("Z", "+00:00"))

    return {
        "event_id": ev.get("id", ""),
        "summary": summary or None,
        "leave_type": parsed["leave_type"],
        "team": parsed["team"],
        "person": parsed["person"],
        "person_note": parsed["person_note"],
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "is_all_day": is_all_day,
        "creator_email": (ev.get("creator") or {}).get("email"),
        "event_created": event_created,
        "event_updated": event_updated,
        "run_id": run_id,
    }


# ------------------------------------------------------------------
# AI Normalization: team + leave_type
# ------------------------------------------------------------------

# Official leave type codes and their meanings
_OFFICIAL_LEAVE_TYPES = {
    "RDOT": "Rest Day Overtime",
    "RDO": "Rest Day Offset",
    "RD": "Rest Day",
    "VL": "Vacation Leave",
    "SL": "Sick Leave",
    "EL": "Emergency Leave",
    "SDL": "Sudden Leave",
    "UT": "Undertime",
    "BL": "Birthday Leave",
    "ML": "Maternity Leave",
    "PL": "Paternity Leave",
    "SPL": "Solo Parent Leave",
    "STL": "Student Leave",
    "BRL": "Bereavement Leave",
    "LR": "Weekend Live Review",
    "WW": "Weekend Work",
    "LAC": "Lack of Attendance Credit",
    "HD": "Half Day",
    "PH": "Public Holiday",
    "LWOP": "Leave Without Pay",
}


def _build_team_prompt(raw_teams: list[str]) -> str:
    return f"""You are normalizing team/group names from a company's leave calendar.

These are all the distinct raw team values found in the data. Many are the same team spelled differently (typos, abbreviations, case differences).

Map each raw value to a single canonical team name. Rules:
- Use the most common/proper spelling as the canonical name
- Merge obvious duplicates: "ACCTG" and "Acctg" and "Accounting" should all map to "Acctg"
- "Admin and Ops" and "Admin & Ops" should map to "Admin and Ops"
- "T&A", "TNA", "Tools and Automation", "Tools & Automation", "Tools&Auto" should map to "T&A"
- "Marketing" and "MKTG" should map to "MKTG"
- "CRTV" and "CRTVS" should map to "CRTV"
- "PHI DS" and "PHIDS" should map to "PHI DS"
- "Swifttt" and "Swift" should map to "Swift"
- "GC2" is likely a typo for "CG2"
- If a value looks like a person's name (not a team), map it to null
- If unsure, keep the original value

Raw team values:
{json.dumps(raw_teams, indent=2)}

Return ONLY a JSON object mapping each raw value to its normalized form (or null for non-team values).
Example: {{"ACCTG": "Acctg", "Acctg": "Acctg", "Gabby": null}}"""


def _build_leave_type_prompt(raw_types: list[str]) -> str:
    official = json.dumps(_OFFICIAL_LEAVE_TYPES, indent=2)
    return f"""You are normalizing leave type codes from a company's leave calendar.

Official leave type codes:
{official}

Map each raw leave_type value to the correct official code. Rules:
- Compound types like "UT/SL" should stay as "UT/SL" (use "/" separator, no spaces)
- "UT SL" should become "UT/SL"
- "VL / LAC" and "VL (LAC)" should become "VL/LAC"
- "Weekend Work" and "Weekend work" should become "WW"
- "Birthday Leave" should become "BL"
- "Half Day" and "Half Day SL" → use "HD" for half day, "HD/SL" for half day sick leave
- "SL (Half day)" → "HD/SL"
- "UT (HD)" → "UT/HD"
- "LAC (2)" → "LAC"
- "PH Holiday" → "PH"
- Holiday descriptions like "PH: Christmas Day" → "PH"
- "Lunar New Year's Day" → "PH"
- Entries that look like personal events (birthdays, performance evaluations) → map to null
- "VlL" is a typo for "VL"
- If a value looks like a team name or person name accidentally in the leave_type field → null
- If unsure, keep the original value

Raw leave_type values:
{json.dumps(raw_types, indent=2)}

Return ONLY a JSON object mapping each raw value to its normalized code (or null for non-leave entries).
Example: {{"UT/SL": "UT/SL", "Weekend Work": "WW", "Ced's Birthday!": null}}"""


def ai_normalize(raw_teams: list[str], raw_leave_types: list[str]) -> tuple[dict, dict]:
    """Call Claude to normalize team names and leave type codes.

    Returns (team_map, leave_type_map) dicts mapping raw -> normalized.
    """
    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        logger.warning("CLAUDE_API_KEY not set -- skipping AI normalization")
        return {}, {}

    client = anthropic.Anthropic(api_key=api_key)

    # Normalize teams
    logger.info(f"  AI normalizing {len(raw_teams)} distinct team values...")
    team_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": _build_team_prompt(raw_teams)}],
    )
    team_text = team_resp.content[0].text.strip()
    # Extract JSON from response (may be wrapped in ```json blocks)
    team_text = re.sub(r"^```(?:json)?\s*", "", team_text)
    team_text = re.sub(r"\s*```$", "", team_text)
    team_map = json.loads(team_text)
    logger.info(f"  Team normalization: {len(team_map)} mappings")

    # Normalize leave types
    logger.info(f"  AI normalizing {len(raw_leave_types)} distinct leave_type values...")
    ltype_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": _build_leave_type_prompt(raw_leave_types)}],
    )
    ltype_text = ltype_resp.content[0].text.strip()
    ltype_text = re.sub(r"^```(?:json)?\s*", "", ltype_text)
    ltype_text = re.sub(r"\s*```$", "", ltype_text)
    leave_type_map = json.loads(ltype_text)
    logger.info(f"  Leave type normalization: {len(leave_type_map)} mappings")

    return team_map, leave_type_map


def transform_to_staging(db, run_id: str, events: list, full_refresh: bool = False):
    """Parse raw events and load into staging.

    Args:
        full_refresh: If True, truncate staging first and INSERT all rows.
                      If False (default), UPSERT on event_id (update existing, insert new).
    """
    logger.info("Transforming to staging...")

    if full_refresh:
        retry_db(
            lambda: db.execute(f"TRUNCATE TABLE {SCHEMA_STAGING}.stg_calendar_leave RESTART IDENTITY"),
            description="truncate stg_calendar_leave",
        )

    # Parse all events
    rows = []
    parse_errors = 0
    for ev in events:
        try:
            rows.append(parse_event(ev, run_id))
        except Exception as e:
            parse_errors += 1
            logger.warning(f"  Parse error for event {ev.get('id', '?')}: {e}")

    if parse_errors:
        logger.warning(f"  {parse_errors} events failed to parse")

    # AI normalization: collect distinct values, get mappings
    raw_teams = sorted(set(r["team"] for r in rows if r["team"]))
    raw_leave_types = sorted(set(r["leave_type"] for r in rows if r["leave_type"]))

    team_map, leave_type_map = ai_normalize(raw_teams, raw_leave_types)

    # Apply normalization
    for r in rows:
        r["team_normalized"] = team_map.get(r["team"]) if r["team"] else None
        r["leave_type_normalized"] = leave_type_map.get(r["leave_type"]) if r["leave_type"] else None

    # Build SQL — upsert for incremental, plain insert for full refresh
    if full_refresh:
        sql = (
            f"INSERT INTO {SCHEMA_STAGING}.stg_calendar_leave "
            f"(event_id, summary, leave_type, team, person, person_note, "
            f"start_date, end_date, days, is_all_day, creator_email, "
            f"event_created, event_updated, run_id, "
            f"team_normalized, leave_type_normalized) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)"
        )
    else:
        sql = (
            f"INSERT INTO {SCHEMA_STAGING}.stg_calendar_leave "
            f"(event_id, summary, leave_type, team, person, person_note, "
            f"start_date, end_date, days, is_all_day, creator_email, "
            f"event_created, event_updated, run_id, "
            f"team_normalized, leave_type_normalized) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) "
            f"ON CONFLICT (event_id) DO UPDATE SET "
            f"summary = EXCLUDED.summary, "
            f"leave_type = EXCLUDED.leave_type, "
            f"team = EXCLUDED.team, "
            f"person = EXCLUDED.person, "
            f"person_note = EXCLUDED.person_note, "
            f"start_date = EXCLUDED.start_date, "
            f"end_date = EXCLUDED.end_date, "
            f"days = EXCLUDED.days, "
            f"is_all_day = EXCLUDED.is_all_day, "
            f"creator_email = EXCLUDED.creator_email, "
            f"event_created = EXCLUDED.event_created, "
            f"event_updated = EXCLUDED.event_updated, "
            f"run_id = EXCLUDED.run_id, "
            f"team_normalized = EXCLUDED.team_normalized, "
            f"leave_type_normalized = EXCLUDED.leave_type_normalized, "
            f"loaded_at = now()"
        )

    # Batch load
    for i in range(0, len(rows), LOAD_BATCH_SIZE):
        batch = rows[i:i + LOAD_BATCH_SIZE]
        tuples = [
            (
                r["event_id"], r["summary"], r["leave_type"], r["team"],
                r["person"], r["person_note"], r["start_date"], r["end_date"],
                r["days"], r["is_all_day"], r["creator_email"],
                r["event_created"], r["event_updated"], r["run_id"],
                r["team_normalized"], r["leave_type_normalized"],
            )
            for r in batch
        ]
        retry_db(
            lambda t=tuples: db.executemany(sql, t),
            description=f"{'insert' if full_refresh else 'upsert'} stg_calendar_leave batch {i // LOAD_BATCH_SIZE + 1}",
        )

    logger.info(f"  {'Loaded' if full_refresh else 'Upserted'} {len(rows)} staging rows ({parse_errors} parse errors)")
    return len(rows)


# ------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------

def _run_pipeline_core(full_refresh: bool = False):
    """Core pipeline logic (called inside log capture wrapper)."""
    db = get_db()
    run_id = str(uuid.uuid4())
    mode = "FULL REFRESH" if full_refresh else "INCREMENTAL"
    logger.info(f"Run ID: {run_id}")

    start_pipeline_run(db, run_id, metadata={"calendar_id": CALENDAR_ID, "mode": mode})

    try:
        # 1. Authenticate with Google Calendar
        logger.info("Authenticating with Google Calendar...")
        service = authenticate_calendar()
        logger.info("Authenticated successfully")

        # 2. Determine fetch window
        updated_min = None
        if not full_refresh:
            updated_min = get_last_updated(db)
            if not updated_min:
                logger.info("No existing data found -- switching to full refresh")
                full_refresh = True

        # 3. Fetch events
        logger.info("Fetching events from Leave calendar...")
        events = fetch_events(service, updated_min=updated_min)
        logger.info(f"Total events fetched: {len(events)}")

        if not events:
            logger.info("No new/updated events found. Nothing to process.")
            complete_pipeline_run(db, run_id, "success", records=0)
            return

        # 4. Load raw events (always append)
        logger.info("Loading raw events...")
        load_raw_events(db, run_id, events)

        # 5. Transform to staging (upsert or truncate+reload)
        staging_count = transform_to_staging(db, run_id, events, full_refresh=full_refresh)

        # 6. Complete
        complete_pipeline_run(db, run_id, "success", records=staging_count)

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Calendar Leave Pipeline Complete")
        logger.info(f"  Raw events loaded: {len(events)}")
        logger.info(f"  Staging rows {'loaded' if full_refresh else 'upserted'}: {staging_count}")
        logger.info(f"{'=' * 60}\n")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        complete_pipeline_run(db, run_id, "failed", error=str(e)[:500])
        raise


def run_calendar_leave_pipeline(full_refresh: bool = False, send_email: bool = True):
    """Main entry point with log capture and email notification.

    Args:
        full_refresh: If True, re-fetch all events and truncate+reload staging.
        send_email: If True (default), send email notification on completion/failure.
    """
    setup_logging()
    mode = "FULL REFRESH" if full_refresh else "INCREMENTAL"
    run_label = f"Calendar Leave ({mode})"

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Calendar Leave Pipeline ({mode})")
    logger.info(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'=' * 60}\n")

    tables = PIPELINE_TABLES.get("Calendar Leave")
    started_at = datetime.now(timezone.utc)
    row_counts_before = snapshot_row_counts(tables)

    with capture_logs() as log_handler:
        try:
            _run_pipeline_core(full_refresh=full_refresh)
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)

            if send_email:
                result = PipelineResult(
                    pipeline_name="Calendar Leave",
                    status="SUCCESS",
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                )
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="SUCCESS",
                    run_label=run_label,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )

        except Exception as e:
            ended_at = datetime.now(timezone.utc)
            duration = (ended_at - started_at).total_seconds()
            row_counts_after = snapshot_row_counts(tables)

            if send_email:
                result = PipelineResult(
                    pipeline_name="Calendar Leave",
                    status="FAILED",
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration,
                    error_message=str(e),
                )
                send_pipeline_email(
                    results=[result],
                    log_output=log_handler.get_log_output(),
                    overall_status="FAILED",
                    run_label=run_label,
                    started_at=started_at,
                    ended_at=ended_at,
                    total_duration=duration,
                    row_counts_before=row_counts_before,
                    row_counts_after=row_counts_after,
                    row_count_tables=tables,
                )
            raise
        finally:
            close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract calendar leave data from Google Calendar")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Re-fetch all events and truncate+reload staging (default: incremental)")
    parser.add_argument("--no-email", action="store_true",
                        help="Suppress email notification")
    args = parser.parse_args()
    run_calendar_leave_pipeline(full_refresh=args.full_refresh, send_email=not args.no_email)
