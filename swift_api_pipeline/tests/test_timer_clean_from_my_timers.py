# swift_api_pipeline/tests/test_timer_clean_from_my_timers.py
"""Contract tests for migration 288: the old clean table is refilled from My Timers.

From 2026-11-04 data_staging.stg_timer_activities_clean (every report reads it) is built from
data_staging.stg_timer_entries_clean (My Timers' answer: member fixes, the 10-hour cap, exact
duplicates counted once) instead of the old email forms. These tests pin, on the migration text:

  1. the house header (STATUS, WHY, PREFLIGHT, ROLLBACK, VERIFY);
  2. the advisory lock is the first statement, search_path is pinned, the function stays void;
  3. the refusal guard runs before any write (an empty or short My Timers table never empties
     the reports);
  4. rows before 2025-09-01 PHT are never deleted (only the cutoff or the My Timers run_id);
  5. no TRUNCATE and no full DELETE of the real table; the build happens in a temp table;
  6. the sync compares every column but id and loaded_at (the asset_id drift bug class);
  7. duplicates are left out, running entries keep end NULL and 0 minutes, the cap applies to
     both the minutes and the end (the hours rollup measures start to end);
  8. ids are derived from swift_timer_id above 2^61 (never collide with the old serial ids);
  9. the hourly cron only calls the rebuild.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(os.path.dirname(HERE), "migrations", "288_timer_clean_from_my_timers.sql")

CLEAN_COLUMNS = ["id", "project", "project_number", "project_did", "site_name", "site_id", "task",
                 "site_lat", "site_long", "user_lat", "user_long", "user_accuracy_m",
                 "site_vs_user_km", "start_time", "end_time", "duration_min", "user_name",
                 "user_email", "user_role", "run_id", "run_date", "loaded_at", "start_date",
                 "end_date", "task_clean", "asset_did"]
CUTOFF = "timestamptz '2025-08-31 16:00:00+00'"
RUN_ID = "'00000000-0000-0000-0000-000000000003'::uuid"


def _sql():
    with open(MIG, encoding="utf-8") as f:
        return f.read()


def _code():
    return "\n".join(re.sub(r"--.*$", "", line) for line in _sql().splitlines())


def _body():
    code = _code()
    fs = code.index("create or replace function data_staging.rebuild_timer_clean()")
    return code[fs:code.index("$function$;", fs)]


def _statements():
    body = _body()
    return [s.strip() for s in body[body.index("begin\n"):].split(";") if s.strip()]


def test_header_has_the_house_sections():
    head = _sql()[:_sql().index("do $preflight$")]
    for word in ("STATUS:", "WHY", "PREFLIGHT", "ROLLBACK", "VERIFY"):
        assert word in head, word


def test_lock_first_search_path_pinned_void():
    body = _body()
    assert "returns void" in body
    assert "set search_path = ''" in body
    first = _statements()[0]
    assert "pg_advisory_xact_lock(hashtext('data_staging.rebuild_timer_clean'))" in first


def test_refusal_guard_before_any_write():
    body = _body()
    guard = body.index("raise exception")
    for write in ("insert into pg_temp.tmp_timer_clean", "delete from data_staging.stg_timer_activities_clean",
                  "insert into data_staging.stg_timer_activities_clean"):
        assert body.index(write) > guard, write
    assert "v_src < 0.9 * v_cur" in body


def test_older_rows_are_never_deleted():
    body = _body()
    delete = body[body.index("delete from data_staging.stg_timer_activities_clean"):]
    delete = delete[:delete.index(";")]
    assert "where (c.start_time >= c_cutoff or c.run_id = c_run_id)" in delete
    assert f"c_cutoff constant timestamptz := {CUTOFF}" in body
    assert f"c_run_id constant uuid := {RUN_ID}" in body


def test_no_truncate_no_full_delete_build_in_temp():
    body = _body()
    assert "truncate data_staging" not in body
    assert "delete from data_staging.stg_timer_activities_clean;" not in body
    assert "(like data_staging.stg_timer_activities_clean including defaults) on commit drop" in body


def test_sync_compares_every_column_but_id_and_loaded_at():
    body = _body()
    m = re.search(r"where t\.id = c\.id\s+and \((.*?)\) is not distinct from \((.*?)\)", body, re.S)
    assert m, "compare tuple not found"
    t_cols = [x.strip()[2:] for x in m.group(1).split(",")]
    c_cols = [x.strip()[2:] for x in m.group(2).split(",")]
    assert t_cols == c_cols
    assert set(t_cols) == set(CLEAN_COLUMNS) - {"id", "loaded_at"}


def test_insert_lists_every_column():
    body = _body()
    m = re.search(r"insert into pg_temp\.tmp_timer_clean \((.*?)\)", body, re.S)
    cols = [x.strip() for x in m.group(1).split(",")]
    assert sorted(cols) == sorted(CLEAN_COLUMNS)


def test_duplicates_running_and_cap():
    body = _body()
    assert "where not n.is_duplicate" in body
    assert "when n.is_running then null" in body
    assert "when n.is_capped then n.start_time + interval '600 minutes'" in body
    assert "case when n.is_running then 0 else n.counted_minutes end" in body


def test_ids_from_swift_timer_id_above_2_pow_61():
    body = _body()
    assert "(('x' || substr(md5(n.swift_timer_id), 1, 15))::bit(60)::bigint) + 2305843009213693952" in body
    assert 2305843009213693952 == 2 ** 61


def test_cron_only_calls_the_rebuild():
    code = _code()
    m = re.search(r"cron\.schedule\(\s*'([\w-]+)',\s*'([^']+)',\s*\$cmd\$(.*?)\$cmd\$", code, re.S)
    assert m, "cron.schedule not found"
    assert m.group(1) == "rebuild-timer-clean-hourly"
    assert m.group(2) == "35 * * * *"
    assert m.group(3).strip() == "select data_staging.rebuild_timer_clean()"


def test_function_revoked_from_public_anon_authenticated():
    code = _code()
    assert "revoke all on function data_staging.rebuild_timer_clean() from public, anon, authenticated" in code


def test_project_name_only_from_a_copy_of_the_same_project():
    body = _body()
    for src in ("i", "le", "cur"):
        assert f"case when {src}.project_did = n.project_did then {src}.project end" in body, src
    assert "case when le.project_did = n.project_did then fl.project end" in body
    assert "from data_staging.stg_projects p" in body


def test_forty_day_guard_before_any_write():
    body = _body()
    assert "v_src_40 < 0.9 * v_cur_40" in body
    assert body.index("v_src_40 < 0.9 * v_cur_40") < body.index("insert into pg_temp.tmp_timer_clean")


def test_preflight_and_apply_order_documented():
    sql = _sql()
    assert sql.index("do $preflight$") < sql.index("create or replace function data_staging.rebuild_timer_clean()")
    assert "APPLY ORDER" in sql and "legacy-import" in sql
