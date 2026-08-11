"""DB-backed QA forms registry — replaces the config.py QA_FORMS dict.

Source of truth: reference.ref_qa_forms (migration 231), maintained by seed +
the nightly auto-discovery step (qa_form_discovery.py). Consumers get the same
dict shape the legacy QA_FORMS constant had, so extract/transform code is
unchanged beyond the lookup.
"""
from config import SCHEMA_REFERENCE

_QUERY = f"""
    SELECT ts_number, form_id
    FROM {SCHEMA_REFERENCE}.ref_qa_forms
    WHERE active
    ORDER BY ts_number
"""


def row_to_entry(ts_number, form_id):
    key = f"qa_ts{ts_number}"
    return key, {
        "form_id": form_id,
        "table_name": f"raw_form_qa_ts{ts_number}",
        "display_name": f"QA Form TS{ts_number}",
    }


def load_qa_forms(db):
    rows = db.fetch(_QUERY)
    if not rows:
        raise RuntimeError(
            "reference.ref_qa_forms returned no active rows - refusing to run "
            "the forms pipeline against an empty registry"
        )
    forms = {}
    for r in rows:
        key, entry = row_to_entry(r["ts_number"], r["form_id"])
        forms[key] = entry
    return forms
