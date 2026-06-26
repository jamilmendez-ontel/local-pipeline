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


def build_row(ev: dict, shape: dict, run_id: str, norm: dict) -> dict:
    summary = (ev.get("summary") or "").strip() or None
    start_date, end_date, days, is_all_day = event_dates(ev)
    return {
        "event_id": ev.get("id", ""),
        "ical_uid": ev.get("iCalUID"),
        "summary_raw": summary,
        "event_kind": shape["event_kind"],
        "leave_type": shape["leave_type"],
        "leave_type_normalized": norm["leave_type_normalized"],
        "team": shape["team"],
        "team_normalized": norm["team_normalized"],
        "team_level": norm["team_level"],
        "person": shape["person"],
        "person_normalized": norm["person_normalized"],
        "emp_id": norm["emp_id"],
        "person_match_source": norm["person_match_source"],
        "person_note": shape["person_note"],
        "person_note_normalized": norm["person_note_normalized"],
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
