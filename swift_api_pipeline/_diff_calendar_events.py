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
