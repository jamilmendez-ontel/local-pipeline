"""Auto-discover and register QA forms for new TS projects.

Spike-proven 2026-08-11: GET /api/organizations/{ONTEL_ORG_DID}/forms returns
all org forms (paginated) with standard bearer auth; QA form titles follow the
strict pattern 'ACTIVE - QA Form TS{n}'. Runs at the top of the nightly forms
pipeline. Failure of any kind degrades to an alert email - never blocks the
extraction of already-registered forms.

Deliberate deviation from every-object-via-migration: the raw table for a
newly discovered form is created from RAW_TABLE_DDL below (version-controlled
template) - same precedent as asset-tasks partition auto-creation.
"""
import base64
import re
from email.mime.text import MIMEText

import requests

from config import SWIFT_BASE_URL, SCHEMA_RAW, SCHEMA_REFERENCE, get_logger
from qa_forms_registry import row_to_entry

logger = get_logger("qa_form_discovery")

ONTEL_ORG_DID = "-K5UFaiZw8e3-7nii3eT"
TITLE_PATTERN = re.compile(r"^ACTIVE - QA Form TS(\d+)$")
MIN_TS_NUMBER = 13
ESCALATION_AGE_DAYS = 7
ALERT_RECIPIENT = "jamil.mendez@ontel.co"

RAW_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.{table} (
    id BIGSERIAL PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_id UUID NOT NULL,
    data JSONB NOT NULL
);
ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON {schema}.{table} FROM anon, authenticated;
CREATE INDEX IF NOT EXISTS idx_{table}_run_id ON {schema}.{table}(run_id);
CREATE INDEX IF NOT EXISTS idx_{table}_data ON {schema}.{table} USING GIN(data);
"""


# ---------- pure decision core ----------

def match_qa_form(forms, ts_number):
    matches = [
        {"id": f["id"], "title": f["title"]}
        for f in forms
        if (m := TITLE_PATTERN.match(f.get("title") or ""))
        and int(m.group(1)) == ts_number
    ]
    status = "one" if len(matches) == 1 else ("zero" if not matches else "many")
    return {"status": status, "matches": matches}


def missing_ts_numbers(projects, registered):
    return sorted(
        p["project_number"] for p in projects
        if p["project_number"] not in registered
    )


def needs_escalation(task_count, project_age_days):
    return task_count > 0 and project_age_days > ESCALATION_AGE_DAYS


# ---------- IO ----------

def fetch_org_forms(token):
    url = f"{SWIFT_BASE_URL}/api/organizations/{ONTEL_ORG_DID}/forms"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"pageSize": "100"}
    forms = []
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("list", [])
        forms.extend(batch)
        if not data.get("hasMore") or not batch:
            return forms
        params["after"] = batch[-1]["id"]


def send_alert(subject, body):
    from gmail_client import authenticate
    service = authenticate()
    msg = MIMEText(body, "plain")
    msg["To"] = ALERT_RECIPIENT
    msg["From"] = "me"
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Alert sent: {subject}")


def register_qa_form(db, ts_number, form_id, form_title):
    _, entry = row_to_entry(ts_number, form_id)
    ddl = RAW_TABLE_DDL.format(schema=SCHEMA_RAW, table=entry["table_name"])
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        db.execute(stmt)
    db.execute(
        f"INSERT INTO {SCHEMA_REFERENCE}.ref_qa_forms "
        f"(ts_number, form_id, form_title, table_name, registered_by) "
        f"VALUES ($1, $2, $3, $4, 'auto-discovery') "
        f"ON CONFLICT (ts_number) DO NOTHING",
        ts_number, form_id, form_title, entry["table_name"],
    )
    logger.info(f"Registered QA form TS{ts_number}: {form_id} -> {entry['table_name']}")


def _project_state(db, ts_number):
    rows = db.fetch(
        f"SELECT p.project_did, "
        f"       COALESCE(EXTRACT(EPOCH FROM (NOW() - sp.date_created)) / 86400, 0) AS age_days, "
        f"       (SELECT COUNT(*) FROM data_staging.stg_asset_tasks t "
        f"        WHERE t.project_did = p.project_did) AS task_count "
        f"FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects p "
        f"JOIN data_staging.stg_projects sp ON sp.project_did = p.project_did "
        f"WHERE p.project_number = $1",
        ts_number,
    )
    return rows[0] if rows else None


def run_discovery(db, token, send_email=True):
    """Register QA forms for unregistered TS projects. Returns new ts_numbers.

    Everything before the per-TS loop (the DB reads that determine what's
    missing, and the Swift forms fetch) is one infrastructure step: if any
    of it raises, there is no per-TS scope to isolate, and the 7-day
    escalation below can never fire because it never reaches this failing
    step. That failure is alerted unconditionally here (not gated on "was
    there something to discover") because it's rare and always actionable -
    it means auto-discovery is completely dark until someone looks.
    """
    try:
        projects = db.fetch(
            f"SELECT project_number FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects "
            f"WHERE project_number >= $1", MIN_TS_NUMBER,
        )
        registered = {
            r["ts_number"] for r in
            db.fetch(f"SELECT ts_number FROM {SCHEMA_REFERENCE}.ref_qa_forms")
        }
        missing = missing_ts_numbers(projects, registered)
        if not missing:
            return []

        forms = fetch_org_forms(token)
    except Exception as e:
        logger.error("QA form auto-discovery: infrastructure failure before per-TS loop", exc_info=True)
        if send_email:
            try:
                send_alert(
                    "[forms] QA auto-discovery infrastructure failure",
                    f"QA form auto-discovery failed before it could check individual TS "
                    f"projects:\n\n"
                    f"  {type(e).__name__}: {e}\n\n"
                    f"This blocks discovery of NEW QA forms only - extraction of "
                    f"already-registered forms is unaffected. If this repeats nightly, "
                    f"the per-TS 7-day escalation can never fire because it lives inside "
                    f"this same failing step.",
                )
            except Exception:
                logger.error("QA auto-discovery alert email also failed", exc_info=True)
        return []

    newly = []
    for ts in missing:
        try:
            result = match_qa_form(forms, ts)
            if result["status"] == "one":
                m = result["matches"][0]
                register_qa_form(db, ts, m["id"], m["title"])
                newly.append(ts)
                if send_email:
                    send_alert(
                        f"[forms] Registered QA Form TS{ts} automatically",
                        f"Auto-discovery registered QA Form TS{ts}:\n\n"
                        f"  form_id: {m['id']}\n  title:   {m['title']}\n"
                        f"  table:   raw_form_qa_ts{ts}\n\n"
                        f"It is included in tonight's forms extraction. "
                        f"Reply/flag if this is wrong - deactivate with:\n"
                        f"  UPDATE reference.ref_qa_forms SET active=false WHERE ts_number={ts};",
                    )
            elif result["status"] == "many":
                if send_email:
                    lines = "\n".join(f"  {m['id']}  {m['title']}" for m in result["matches"])
                    send_alert(
                        f"[forms] QA Form TS{ts}: multiple candidates - manual pick needed",
                        f"Auto-discovery found {len(result['matches'])} candidate forms for TS{ts}:\n\n"
                        f"{lines}\n\nInsert the right one:\n"
                        f"  INSERT INTO reference.ref_qa_forms (ts_number, form_id, form_title, table_name, registered_by)\n"
                        f"  VALUES ({ts}, '<form_id>', '<title>', 'raw_form_qa_ts{ts}', 'manual');",
                    )
            else:  # zero - quiet retry unless escalation applies
                state = _project_state(db, ts)
                if state and needs_escalation(state["task_count"], float(state["age_days"])):
                    if send_email:
                        send_alert(
                            f"[forms] TS{ts} has tasks flowing but no QA form after "
                            f"{int(float(state['age_days']))} days",
                            f"TECH-OPS: TS{ts} has {state['task_count']:,} asset-task rows but no "
                            f"'ACTIVE - QA Form TS{ts}' exists in Swift yet.\n"
                            f"Discovery retries nightly; this alert repeats until the form "
                            f"appears or a row is inserted manually.",
                        )
                else:
                    logger.info(f"TS{ts}: no QA form in Swift yet - will retry nightly")
        except Exception:
            logger.error(f"TS{ts}: auto-discovery step failed, skipping to next", exc_info=True)
    return newly
