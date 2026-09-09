# swift_api_pipeline/tests/test_wal_diet_guards.py
"""Contract tests for the change-only merges introduced by the 2026-09-08
WAL diet (daily_reports_merge.py, transform.transform_user_priorities).

The merges only write rows whose compared columns differ, so a column that
the SET clause writes but the compare tuple omits would silently never
update (the 2026-08-06 asset_id drift bug class). These tests pin:

  1. every written column (minus loaded_at and the run bookkeeping columns)
     is compared;
  2. every written column is also inserted for new rows;
  3. the temp table the batch is COPYed into declares exactly the insert
     columns, in order (asyncpg COPY is positional per column list);
  4. the user-priorities merge deletes rows that left the feed and still
     runs as ONE statement (readers must never see a partial table).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import daily_reports_merge as drm
from transform import USER_PRIORITIES_MERGE_SQL, USER_PRIORITY_COLUMNS


def _set_columns(sql):
    clause = re.search(r"DO UPDATE SET(.*?)RETURNING", sql, re.S).group(1)
    return set(re.findall(r"(\w+)\s*=\s*EXCLUDED\.", clause))


def _compared_columns(sql, key_cols):
    clause = re.search(r"changed AS \((.*?)\), ups AS", sql, re.S).group(1)
    where = clause[clause.index("WHERE"):]
    return set(re.findall(r"\bt\.(\w+)", where)) - set(key_cols)


def _insert_columns(sql):
    m = re.search(r"INSERT INTO \S+ \(([^)]*)\)", sql)
    return [c.strip() for c in m.group(1).split(",")]


def _temp_columns(ddl):
    body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
    cols = [part.strip().split()[0] for part in body.split(",")]
    return [c for c in cols if not c.startswith("_")]  # _seq is COPY-filled


MERGES = [
    ("raw", drm.RAW_MERGE_SQL, drm.RAW_TEMP_DDL, drm.RAW_COLUMNS, ["source_type", "source_id"]),
    ("tasks", drm.TASKS_MERGE_SQL, drm.TASKS_TEMP_DDL, drm.TASKS_COLUMNS, ["task_did"]),
    ("hours", drm.HOURS_MERGE_SQL, drm.HOURS_TEMP_DDL, drm.HOURS_COLUMNS, ["task_did", "req_id"]),
    ("timers", drm.TIMERS_MERGE_SQL, drm.TIMERS_TEMP_DDL, drm.TIMERS_COLUMNS, ["task_did", "timer_id"]),
]


def test_daily_reports_merges_compare_every_written_column():
    for name, sql, _ddl, _cols, keys in MERGES:
        written = _set_columns(sql) - {"loaded_at"} - set(drm.BOOKKEEPING)
        compared = _compared_columns(sql, keys)
        assert written == compared, (
            f"{name}: written but never compared {sorted(written - compared)}; "
            f"compared but never written {sorted(compared - written)}"
        )


def test_daily_reports_merges_insert_every_written_column():
    for name, sql, _ddl, cols, _keys in MERGES:
        inserted = _insert_columns(sql)
        assert inserted == cols, f"{name}: INSERT column list drifted from {name.upper()}_COLUMNS"
        assert _set_columns(sql) - {"loaded_at"} <= set(inserted), f"{name}: SET writes a column not inserted"


def test_daily_reports_temp_tables_match_copy_columns():
    for name, _sql, ddl, cols, _keys in MERGES:
        assert _temp_columns(ddl) == cols, f"{name}: temp table columns != COPY column list"
        assert "ON COMMIT DROP" in ddl, f"{name}: temp table must drop at commit"
        assert "_seq bigserial" in ddl, f"{name}: needs the _seq tiebreak column"


def test_daily_reports_merges_last_duplicate_wins():
    for name, sql, _ddl, _cols, _keys in MERGES:
        assert ", _seq DESC" in sql, f"{name}: in-batch duplicate must resolve to the LAST row sent"


def test_daily_reports_raw_compare_normalises_legacy_json_strings():
    # Legacy rows are double-encoded jsonb strings; comparing them as text
    # against a real object would rewrite the whole table once.
    assert "jsonb_typeof(t.data) = 'string'" in drm.RAW_MERGE_SQL
    assert "data::jsonb AS data" in drm.RAW_MERGE_SQL


def test_daily_reports_merges_select_only_changed_rows():
    for name, sql, _ddl, _cols, keys in MERGES:
        assert f"t.{keys[0]} IS NULL OR" in sql, f"{name}: new-row test missing"
        assert "LEFT JOIN" in sql, f"{name}: must LEFT JOIN the target to filter unchanged rows"
        assert "DISTINCT ON" in sql, f"{name}: in-batch duplicates would break ON CONFLICT"


def test_user_priorities_merge_compares_every_written_column():
    sql = USER_PRIORITIES_MERGE_SQL
    written = _set_columns(sql) - {"loaded_at", "run_id"}
    compared = _compared_columns(sql, ["task_did"])
    assert written == compared, (
        f"priorities: written but never compared {sorted(written - compared)}; "
        f"compared but never written {sorted(compared - written)}"
    )
    data_cols = {c for c, _ in USER_PRIORITY_COLUMNS} - {"task_did"}
    assert written == data_cols, "priorities: SET clause drifted from USER_PRIORITY_COLUMNS"


def test_user_priorities_merge_is_one_statement_with_delete():
    sql = USER_PRIORITIES_MERGE_SQL
    assert sql.startswith("WITH src AS (")
    assert "DELETE FROM data_staging.stg_user_priorities" in sql
    assert "NOT EXISTS (SELECT 1 FROM src WHERE src.task_did = g.task_did)" in sql
    assert "g.task_did IS NULL" in sql, "NULL task_did rows cannot be matched and must be replaced"
    assert "ON CONFLICT (task_did) DO UPDATE" in sql
    assert sql.count(";") == 0, "must stay a single statement (no partial-table window for readers)"
    inserted = _insert_columns(sql)
    assert inserted == [c for c, _ in USER_PRIORITY_COLUMNS] + ["run_id"]


def test_user_priorities_transform_keeps_the_clean_name_regexes():
    # Behaviour parity with the pre-merge transform (prefix "1. 2a. " and
    # trailing " 123" are stripped from Task Name).
    clean = dict(USER_PRIORITY_COLUMNS)["task_name_clean"]
    assert r"'^([0-9]+[a-zA-Z]?\. *)+'" in clean
    assert r"'\s+[0-9]+$'" in clean


def test_pipeline_db_has_copy_merge():
    import inspect
    from db import PipelineDB
    sig = inspect.signature(PipelineDB.copy_merge)
    assert list(sig.parameters)[:6] == ["self", "temp_ddl", "temp_table", "columns", "records", "merge_sql"]
