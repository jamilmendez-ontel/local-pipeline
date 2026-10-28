#!/usr/bin/env python3
"""One-time backfill of stg_calendar_events.person_note_normalized (Calendar
Phase 2 Wave 2, INIT-397). DRY RUN BY DEFAULT: prints how many rows would
change and sample before/after pairs. Writes only with --apply.

New and changed events are filled by the normal pipeline run (calendar_events_load
._enrich). This script fixes the rows loaded before Wave 2 shipped. It touches
ONLY person_note_normalized, in one bulk UPDATE, and only rows whose value differs.

It also re-points the person on rows whose note is a roster employee id (the id typed
at the end of the event is the reference for who is on leave; agreed 2026-10-28 by
Jamil Mendez): emp_id, person_normalized, person_match_source = 'id_suffix', and the
person-derived team_normalized / team_level. Second bulk UPDATE, differing rows only.

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

from calendar_normalize import normalize_person_note, normalize_team, id_suffix_match

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


PERSON_COLS = ("emp_id", "person_normalized", "person_match_source", "team_normalized", "team_level")


def plan_person(rows, lookups):
    """rows: dicts with event_id, person_note, team + the PERSON_COLS as stored.
    Returns [(event_id, old_tuple, new_tuple)] for rows the id suffix re-points."""
    out = []
    for r in rows:
        m = id_suffix_match(r["person_note"], lookups["emp_by_id"])
        if m is None:
            continue
        emp = lookups["emp_by_id"][m["emp_id"]]
        team_norm, team_level = normalize_team(emp, r.get("team"), lookups["team_map"])
        new = (m["emp_id"], m["person_normalized"], m["match_source"], team_norm, team_level)
        old = tuple(r.get(c) for c in PERSON_COLS)
        if new != old:
            out.append((r["event_id"], old, new))
    return out


def report_person(pchanges):
    print(f"\nPerson re-pointed by the id suffix: {len(pchanges)} rows")
    for eid, old, new in pchanges[:SAMPLES]:
        print(f"  {eid}: {old[0]} {old[1]!r} ({old[2]}) -> {new[0]} {new[1]!r}")


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
        from calendar_lookups import load_lookups
        rows = db.fetch(
            "SELECT event_id, person_note, person_note_normalized AS current, team, "
            "       emp_id, person_normalized, person_match_source, team_normalized, team_level "
            "FROM data_staging.stg_calendar_events")
        changes, stats = plan(rows)
        pchanges = plan_person(rows, load_lookups(db))
        print("APPLY" if a.apply else "DRY RUN (no writes; pass --apply to write)")
        report(changes, stats)
        report_person(pchanges)
        if not a.apply:
            return
        if pchanges:
            cols = list(zip(*[(c[0],) + c[2] for c in pchanges]))
            retry_db(lambda: db.execute(
                "UPDATE data_staging.stg_calendar_events AS s SET "
                "  emp_id = d.eid, person_normalized = d.pn, person_match_source = d.pms, "
                "  team_normalized = d.tn, team_level = d.tl "
                "FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[]) "
                "  AS d(event_id, eid, pn, pms, tn, tl) "
                "WHERE s.event_id = d.event_id",
                *[list(c) for c in cols]), description=f"re-point person by id suffix ({len(pchanges)} rows)")
            print(f"\nRe-pointed {len(pchanges)} rows.")
        if not changes:
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
