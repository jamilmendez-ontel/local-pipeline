"""Resolve a calendar person to a canonical employee, caching the result in
agent.calendar_person_match keyed on (person_raw, team_raw). Cache -> deterministic
match -> Haiku fallback for the tail. Mirrors calendar_parse_cache."""
import os
import re
import json

from calendar_normalize import match_person_deterministic

MODEL = "claude-haiku-4-5-20251001"


def _unmatched():
    return {"emp_id": None, "person_normalized": None, "confidence": 0.0,
            "match_source": "unmatched"}


def _write_cache(db, person_raw, team_raw, result):
    db.execute(
        "INSERT INTO agent.calendar_person_match "
        "(person_raw, team_raw, emp_id, person_normalized, confidence, match_source) "
        "VALUES ($1,$2,$3,$4,$5,$6) "
        "ON CONFLICT (person_raw, team_raw) DO UPDATE SET "
        "  emp_id=EXCLUDED.emp_id, person_normalized=EXCLUDED.person_normalized, "
        "  confidence=EXCLUDED.confidence, match_source=EXCLUDED.match_source, "
        "  resolved_at=now()",
        person_raw, team_raw, result["emp_id"], result["person_normalized"],
        result["confidence"], result["match_source"],
    )


def extract_person_with_ai(person_raw, team_raw, candidate_names):
    """Ask Haiku which employee (from candidate_names) the calendar name refers to.
    Returns {emp_id?, person_normalized, confidence} or None. Never raises."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        prompt = (
            "Match a name from a shared work calendar to the correct employee.\n"
            f"Calendar name: {json.dumps(person_raw)}\n"
            f"Team on the entry: {json.dumps(team_raw)}\n"
            f"Candidate full names: {json.dumps(candidate_names)}\n\n"
            "Return ONLY a JSON object: {\"person_normalized\": <one candidate full name "
            "or null>, \"confidence\": <0..1>}. Use null if no candidate is a confident match."
        )
        resp = client.messages.create(model=MODEL, max_tokens=128,
                                      messages=[{"role": "user", "content": prompt}])
        text = resp.content[0].text
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        d = json.loads(m.group(0))
        name = d.get("person_normalized")
        if not name:
            return None
        return {"person_normalized": name, "confidence": float(d.get("confidence") or 0.5)}
    except Exception:
        return None


def resolve_person(db, person_raw, team_raw, emp_index, team_map, ai_fn=extract_person_with_ai):
    """Cache -> deterministic -> AI. Returns dict with emp_id, person_normalized,
    confidence, match_source. NULL person is unmatched and not cached."""
    if not person_raw or not person_raw.strip():
        return _unmatched()
    tkey = team_raw or ""
    cached = db.fetchrow(
        "SELECT emp_id, person_normalized, confidence, match_source "
        "FROM agent.calendar_person_match WHERE person_raw=$1 AND team_raw=$2",
        person_raw, tkey)
    if cached is not None:
        return {"emp_id": cached["emp_id"], "person_normalized": cached["person_normalized"],
                "confidence": cached["confidence"], "match_source": cached["match_source"]}

    emp, _source = match_person_deterministic(person_raw, team_raw, emp_index, team_map)
    if emp is not None:
        result = {"emp_id": emp.get("emp_id"), "person_normalized": emp.get("full_name"),
                  "confidence": 1.0, "match_source": "exact"}
    else:
        candidate_names = sorted({e.get("full_name") for e in
                                  sum(emp_index.values(), []) if e.get("full_name")})
        ai = ai_fn(person_raw, team_raw, candidate_names)
        if ai and ai.get("person_normalized"):
            # resolve the AI-chosen full name back to an emp_id via the index
            chosen = ai["person_normalized"]
            matches = emp_index.get(chosen.strip().lower(), [])
            emp_id = matches[0].get("emp_id") if matches else None
            result = {"emp_id": emp_id, "person_normalized": chosen,
                      "confidence": ai.get("confidence", 0.5), "match_source": "ai"}
        else:
            result = _unmatched()

    _write_cache(db, person_raw, tkey, result)
    return result
