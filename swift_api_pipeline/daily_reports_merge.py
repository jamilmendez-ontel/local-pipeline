"""Change-only merges for the Daily Reports rolling pipeline.

Why this exists (2026-09-08, WAL diet):

The rolling pipeline runs 288x/day and re-sends the whole ~15k-row window
every run as per-row `INSERT ... ON CONFLICT DO UPDATE ... WHERE (cols) IS
DISTINCT FROM (EXCLUDED.cols)`. The guard does stop the UPDATE, but Postgres
still takes a row lock on EVERY conflicting row before it evaluates the
guard. A tuple lock dirties the heap page, which means a WAL lock record plus
a full-page image after each 5-minute checkpoint. Measured over 4.78 days:
stg_daily_report_hours wrote 8.4 GB of WAL for 343 real row changes, and
raw_daily_reports 38 GB for ~2.2k changed rows per run (~12.5 KB of WAL per
written row, index and full-page-write amplified). ~10 GB/day, a third of the
warehouse's daily WAL.

The fix is to never present an unchanged row to the target table at all:

  1. COPY the whole batch into a session TEMP table (unlogged: zero WAL).
  2. One INSERT ... SELECT that LEFT JOINs the temp rows to the target and
     keeps only rows that are new or whose compared columns differ.
  3. ON CONFLICT DO UPDATE only ever sees rows that really changed.

All three steps run on one connection inside one transaction
(PipelineDB.copy_merge). The compare tuple is generated from the same column
list as the SET clause, so a column can't be written without being compared
(the 2026-08-06 drift bug class; see tests/test_wal_diet_guards.py).

raw_daily_reports.data: the legacy path passed json.dumps(payload) through
db.py's jsonb codec (encoder=json.dumps), so rows landed as JSONB *strings*
(double-encoded) and the change check was a text compare that any key
reorder defeated. Here the payload text is cast with ::jsonb in SQL, so new
rows land as real objects, and the compare normalises legacy string rows
first (same expression reference.harvest_employee_email_aliases uses), so
the format upgrade does not rewrite the table in one go: a legacy row is
rewritten only when its payload really changes.
"""

from config import SCHEMA_RAW, SCHEMA_STAGING, get_logger, retry_db

logger = get_logger("daily_reports_merge")


BOOKKEEPING = ("run_id", "run_date")


def build_merge_sql(*, target, temp_table, key_cols, insert_cols, update_cols,
                    select_exprs=None, compare_exprs=None):
    """Build the change-only merge statement.

    target       schema-qualified target table
    temp_table   the session temp table holding the batch
    key_cols     conflict target (a unique constraint on `target`)
    insert_cols  every column inserted for a NEW row (superset of update_cols)
    update_cols  columns refreshed on an EXISTING row; each is also compared,
                 except the BOOKKEEPING columns (run_id, run_date) which move
                 only when some other column changed
    select_exprs {col: sql} overrides for reading a column out of the temp
                 table (e.g. run_id::uuid, data::jsonb)
    compare_exprs {col: (target_expr, src_expr)} overrides for the change
                 test of one column (e.g. normalising legacy jsonb strings)

    Duplicate keys inside one batch: the LAST row sent wins (ORDER BY _seq
    DESC under DISTINCT ON), matching the old per-row executemany order.
    Every temp table therefore carries a `_seq bigserial` that COPY fills.

    Returns SQL whose single result value is the number of rows written.
    """
    select_exprs = select_exprs or {}
    compare_exprs = compare_exprs or {}
    src_select = ", ".join(
        f"{select_exprs[c]} AS {c}" if c in select_exprs else c for c in insert_cols
    )
    key_join = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
    diffs = []
    for c in update_cols:
        if c in BOOKKEEPING:
            continue
        t_expr, s_expr = compare_exprs.get(c, (f"t.{c}", f"s.{c}"))
        diffs.append(f"{t_expr} IS DISTINCT FROM {s_expr}")
    changed_where = "t.{} IS NULL OR ".format(key_cols[0]) + " OR ".join(diffs)
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols) + ", loaded_at = now()"
    return (
        f"WITH src AS ("
        f"  SELECT DISTINCT ON ({', '.join(key_cols)}) {src_select}"
        f"  FROM {temp_table} ORDER BY {', '.join(key_cols)}, _seq DESC"
        f"), changed AS ("
        f"  SELECT s.* FROM src s"
        f"  LEFT JOIN {target} t ON {key_join}"
        f"  WHERE {changed_where}"
        f"), ups AS ("
        f"  INSERT INTO {target} ({', '.join(insert_cols)})"
        f"  SELECT {', '.join(insert_cols)} FROM changed"
        f"  ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {set_clause}"
        f"  RETURNING 1"
        f") SELECT count(*) FROM ups"
    )


# --------------------------------------------------------------------------
# raw_daily_reports (tasks, requirements, timers share one table)
# --------------------------------------------------------------------------
RAW_TEMP = "tmp_raw_daily_reports"
RAW_TEMP_DDL = (
    f"CREATE TEMP TABLE {RAW_TEMP} ("
    "source_type text, source_id text, project_did text, asset_did text, "
    "task_did text, data text, run_id text, run_date date, _seq bigserial) ON COMMIT DROP"
)
RAW_COLUMNS = ["source_type", "source_id", "project_did", "asset_did",
               "task_did", "data", "run_id", "run_date"]
# Legacy rows are jsonb strings holding JSON text; new rows are objects.
_LEGACY_JSON = ("(CASE WHEN jsonb_typeof(t.data) = 'string' "
                "THEN (t.data #>> '{}')::jsonb ELSE t.data END)")
RAW_MERGE_SQL = build_merge_sql(
    target=f"{SCHEMA_RAW}.raw_daily_reports",
    temp_table=RAW_TEMP,
    key_cols=["source_type", "source_id"],
    insert_cols=RAW_COLUMNS,
    update_cols=["data", "project_did", "asset_did", "task_did", "run_id", "run_date"],
    select_exprs={"data": "data::jsonb", "run_id": "run_id::uuid"},
    compare_exprs={"data": (_LEGACY_JSON, "s.data")},
)


# --------------------------------------------------------------------------
# stg_daily_reports (one row per task)
# --------------------------------------------------------------------------
TASKS_TEMP = "tmp_stg_daily_reports"
TASKS_TEMP_DDL = (
    f"CREATE TEMP TABLE {TASKS_TEMP} ("
    "emp_id text, asset_name text, asset_did text, project_did text, "
    "work_date date, task_did text, task_status text, req_count integer, "
    "milestone text, submitted_by text, submitted_on timestamptz, "
    "approved_by text, approved_on timestamptz, assigned_approver text, "
    "run_id text, _seq bigserial) ON COMMIT DROP"
)
TASKS_COLUMNS = ["emp_id", "asset_name", "asset_did", "project_did", "work_date",
                 "task_did", "task_status", "req_count", "milestone", "submitted_by",
                 "submitted_on", "approved_by", "approved_on", "assigned_approver",
                 "run_id"]
# emp_id / work_date / asset_did / project_did / milestone are deliberately
# NOT refreshed on an existing row (emp_id derives from the shortName suffix
# and a malformed rename must not overwrite a correct id; see the pre-2026-09
# upsert comment). asset_name IS refreshed: it carries Swift renames.
TASKS_UPDATE = ["asset_name", "task_status", "req_count", "submitted_by",
                "submitted_on", "approved_by", "approved_on", "assigned_approver",
                "run_id"]
TASKS_MERGE_SQL = build_merge_sql(
    target=f"{SCHEMA_STAGING}.stg_daily_reports",
    temp_table=TASKS_TEMP,
    key_cols=["task_did"],
    insert_cols=TASKS_COLUMNS,
    update_cols=TASKS_UPDATE,
    select_exprs={"run_id": "run_id::uuid"},
)


# --------------------------------------------------------------------------
# stg_daily_report_hours (one row per requirement)
# --------------------------------------------------------------------------
# hours_worked / duration_min temp columns are numeric, NOT float8: asyncpg's
# binary numeric codec stores a Python float as its exact binary expansion
# (5037.47999999999956...), which is what the legacy path stored, and a
# float8 round trip would report 27 unchanged rows as changed (measured).
HOURS_TEMP = "tmp_stg_daily_report_hours"
HOURS_TEMP_DDL = (
    f"CREATE TEMP TABLE {HOURS_TEMP} ("
    "emp_id text, work_date date, task_did text, hours_worked numeric, "
    "work_description text, req_status text, req_id text, "
    "created_at_api timestamptz, updated_at_api timestamptz, "
    "file_uploaded_count integer, run_id text, _seq bigserial) ON COMMIT DROP"
)
HOURS_COLUMNS = ["emp_id", "work_date", "task_did", "hours_worked", "work_description",
                 "req_status", "req_id", "created_at_api", "updated_at_api",
                 "file_uploaded_count", "run_id"]
HOURS_UPDATE = ["hours_worked", "work_description", "req_status", "updated_at_api",
                "file_uploaded_count", "run_id"]
HOURS_MERGE_SQL = build_merge_sql(
    target=f"{SCHEMA_STAGING}.stg_daily_report_hours",
    temp_table=HOURS_TEMP,
    key_cols=["task_did", "req_id"],
    insert_cols=HOURS_COLUMNS,
    update_cols=HOURS_UPDATE,
    select_exprs={"run_id": "run_id::uuid"},
)


# --------------------------------------------------------------------------
# stg_daily_report_attendance (one row per timer)
# --------------------------------------------------------------------------
TIMERS_TEMP = "tmp_stg_daily_report_attendance"
TIMERS_TEMP_DDL = (
    f"CREATE TEMP TABLE {TIMERS_TEMP} ("
    "emp_id text, work_date date, task_did text, timer_id text, "
    "timer_start timestamptz, timer_end timestamptz, duration_min numeric, "
    "user_name text, user_auth_id text, run_id text, _seq bigserial) ON COMMIT DROP"
)
TIMERS_COLUMNS = ["emp_id", "work_date", "task_did", "timer_id", "timer_start",
                  "timer_end", "duration_min", "user_name", "user_auth_id", "run_id"]
TIMERS_UPDATE = ["timer_start", "timer_end", "duration_min", "run_id"]
TIMERS_MERGE_SQL = build_merge_sql(
    target=f"{SCHEMA_STAGING}.stg_daily_report_attendance",
    temp_table=TIMERS_TEMP,
    key_cols=["task_did", "timer_id"],
    insert_cols=TIMERS_COLUMNS,
    update_cols=TIMERS_UPDATE,
    select_exprs={"run_id": "run_id::uuid"},
)


def _merge(db, *, label, temp_ddl, temp_table, columns, records, merge_sql):
    """COPY `records` into the temp table and merge the changed rows. Returns
    the number of rows actually written (new + changed)."""
    if not records:
        logger.info(f"  {label}: nothing to load")
        return 0
    written = retry_db(
        lambda: db.copy_merge(temp_ddl, temp_table, columns, records, merge_sql),
        description=f"{label} merge",
    )
    logger.info(f"  {label}: {len(records):,} sent, {written:,} written (new or changed)")
    return written


def merge_raw(db, records, label="raw"):
    return _merge(db, label=label, temp_ddl=RAW_TEMP_DDL, temp_table=RAW_TEMP,
                  columns=RAW_COLUMNS, records=records, merge_sql=RAW_MERGE_SQL)


def merge_tasks(db, records):
    return _merge(db, label="stg tasks", temp_ddl=TASKS_TEMP_DDL, temp_table=TASKS_TEMP,
                  columns=TASKS_COLUMNS, records=records, merge_sql=TASKS_MERGE_SQL)


def merge_hours(db, records):
    return _merge(db, label="stg requirements", temp_ddl=HOURS_TEMP_DDL, temp_table=HOURS_TEMP,
                  columns=HOURS_COLUMNS, records=records, merge_sql=HOURS_MERGE_SQL)


def merge_timers(db, records):
    return _merge(db, label="stg timers", temp_ddl=TIMERS_TEMP_DDL, temp_table=TIMERS_TEMP,
                  columns=TIMERS_COLUMNS, records=records, merge_sql=TIMERS_MERGE_SQL)
