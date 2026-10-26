#!/usr/bin/env python3
"""One-time backfill of stg_calendar_events.person_note_normalized (Calendar
Phase 2 Wave 2, INIT-397). DRY RUN BY DEFAULT: prints how many rows would
change and sample before/after pairs. Writes only with --apply.

New and changed events are filled by the normal pipeline run (calendar_events_load
._enrich). This script fixes the rows loaded before Wave 2 shipped. It touches
ONLY person_note_normalized, in one bulk UPDATE, and only rows whose value differs.

Usage (the direct DB host needs Cloudflare WARP, so run outside a Claude session):
    python _backfill_person_note.py                 # dry run against the DB
    python _backfill_person_note.py --apply         # write
    python _backfill_person_note.py --notes-json f  # offline dry run from an export of
        [{"person_note": ..., "n": ..., "current": ...}] (no DB needed)

`python extract_calendar_events.py --renormalize` also fills the column now, but it
rewrites every normalized column; prefer this script for the note-only backfill.
"""
import argparse
import json
from collections import Counter

from calendar_normalize import normalize_person_note

SAMPLES = 15


def plan(rows):
    """rows: dicts with event_id, person_note, current. Returns (changes, stats)."""
    changes, stats = [], Counter()
    for r in rows:
        stats["rows"] += 1
        if r["person_note"] is not None:
            stats["with_note"] += 1
        new = normalize_person_note(r["person_note"])
        if new is not None:
            stats["would_have_value"] += 1
        elif r["person_note"] is not None:
            stats["note_to_null"] += 1
        if new != r["current"]:
            changes.append((r["event_id"], r["person_note"], r["current"], new))
    stats["would_change"] = len(changes)
    return changes, stats


def report(changes, stats, weight=None):
    for k in ("rows", "with_note", "would_have_value", "note_to_null", "would_change"):
        print(f"  {k:18} {stats[k]}")
    distinct = Counter()
    for _eid, raw, _cur, new in changes:
        distinct[new] += weight.get(raw, 1) if weight else 1
    print(f"  distinct normalized values: {len([k for k in distinct if k])}")
    print(f"\nSample before -> after (up to {SAMPLES}, one per distinct raw note):")
    seen = set()
    for _eid, raw, cur, new in changes:
        if raw in seen or new is None:
            continue
        seen.add(raw)
        print(f"  {raw!r:34} -> {new!r}")
        if len(seen) >= SAMPLES:
            break


def from_json(path):
    data = json.load(open(path, encoding="utf-8"))
    rows, weight = [], {}
    for d in data:
        weight[d["person_note"]] = d["n"]
        for i in range(d["n"]):
            rows.append({"event_id": f"{d['person_note']}#{i}", "person_note": d["person_note"],
                         "current": d.get("current")})
    return rows, weight


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--notes-json", help="offline dry run from an exported note list")
    a = ap.parse_args()

    if a.notes_json:
        if a.apply:
            raise SystemExit("--apply cannot be combined with --notes-json")
        rows, weight = from_json(a.notes_json)
        changes, stats = plan(rows)
        print("DRY RUN (offline export)")
        report(changes, stats, weight)
        return

    from config import get_db, close_db, retry_db
    db = get_db()
    try:
        rows = db.fetch(
            "SELECT event_id, person_note, person_note_normalized AS current "
            "FROM data_staging.stg_calendar_events")
        changes, stats = plan(rows)
        print("APPLY" if a.apply else "DRY RUN (no writes; pass --apply to write)")
        report(changes, stats)
        if not a.apply or not changes:
            return
        ids = [c[0] for c in changes]
        vals = [c[3] for c in changes]
        retry_db(lambda: db.execute(
            "UPDATE data_staging.stg_calendar_events AS s "
            "SET person_note_normalized = d.pnn "
            "FROM unnest($1::text[], $2::text[]) AS d(event_id, pnn) "
            "WHERE s.event_id = d.event_id "
            "  AND s.person_note_normalized IS DISTINCT FROM d.pnn",
            ids, vals), description=f"backfill person_note_normalized ({len(ids)} rows)")
        print(f"\nUpdated {len(ids)} rows.")
    finally:
        close_db()


if __name__ == "__main__":
    main()
