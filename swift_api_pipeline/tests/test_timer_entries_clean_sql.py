# swift_api_pipeline/tests/test_timer_entries_clean_sql.py
"""Contract tests for migration 285 (data_staging.stg_timer_entries_clean).

The table is built from the My Timers app's layout (app_my_timers.entries_layout)
with counted minutes capped at 600. The refresh writes only the rows that differ,
so a column the table has but the compare tuple omits would silently never update
(the 2026-08-06 asset_id drift bug class, same guard as test_wal_diet_guards.py).
These tests pin, on the migration text:

  1. the house header (purpose, STATUS: NOT APPLIED, WHY, PREFLIGHT, ROLLBACK, VERIFY)
     and the hard preflight gates (reader, duplicate emails, pipeline_runs values);
  2. every function pins search_path = '' and is revoked from public, anon, authenticated;
  3. the refresh takes its advisory lock first, compares every column but refreshed_at
     with IS DISTINCT FROM, deletes only rows whose start is inside its own window,
     never truncates, uses one now(), and caps with least(actual, 600);
  4. no legacy_ function is created or reads the new table (no loop with the app);
  5. the two pg_cron commands only call the refresh and the parity snapshot;
  6. RLS on, anon and authenticated revoked, schema_metadata rows for both tables.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(os.path.dirname(HERE), "migrations", "285_stg_timer_entries_clean.sql")

TABLE = "data_staging.stg_timer_entries_clean"
PARITY_TABLE = "data_staging.stg_timer_entries_parity"
REFRESH = "data_staging.refresh_timer_entries_clean"
PARITY_FN = "data_staging.timer_entries_parity"
SNAPSHOT = "data_staging.snapshot_timer_entries_parity"

# Ruling 5 of the plan, in order.
COLUMNS = ["swift_timer_id", "emp_id", "user_email", "project_did", "project_number",
           "asset_did", "site_name", "task_did", "task_clean", "task_name", "start_time",
           "end_time", "work_date", "actual_minutes", "counted_minutes", "is_running",
           "is_capped", "source", "edited_fields", "fixed_by_super_admin", "refreshed_at"]


def _sql():
    with open(MIG, encoding="utf-8") as f:
        return f.read()


def _header():
    sql = _sql()
    return sql[: sql.index("-- ====", 10) + 4] if sql.startswith("-- ====") else ""


def _code():
    """The migration with every -- comment removed (strings here never hold '--')."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in _sql().splitlines())


def _functions():
    """{qualified name: full create statement} for every create function in the code."""
    code = _code()
    out = {}
    for m in re.finditer(r"create (?:or replace )?function ([\w.]+)\s*\(", code, re.I):
        body_start = code.index("$$", m.end())
        body_end = code.index("$$", body_start + 2)
        out[m.group(1).lower()] = code[m.start(): body_end + 2]
    return out


def _table_columns(ddl_name):
    code = _code()
    m = re.search(r"create table " + re.escape(ddl_name) + r"\s*\((.*?)\n\);", code, re.S | re.I)
    assert m, f"create table {ddl_name} not found"
    cols = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or re.match(r"(primary key|constraint|check|unique)\b", line, re.I):
            continue
        cols.append(line.split()[0])
    return cols


def test_header_blocks_present():
    head = _header()
    assert head, "the file must open with the house header block"
    for block in ("285_stg_timer_entries_clean.sql", "STATUS: NOT APPLIED", "WHY",
                  "PREFLIGHT", "ROLLBACK", "VERIFY"):
        assert block in head, f"header lacks {block}"
    for step in ("cron.unschedule('timer-entries-clean-hourly')",
                 "cron.unschedule('timer-entries-clean-nightly')",
                 "cron.unschedule('timer-entries-clean-parity')",
                 f"drop table if exists {TABLE}", f"drop table if exists {PARITY_TABLE}",
                 "delete from agent.schema_metadata"):
        assert step in head, f"ROLLBACK lacks {step}"


def test_no_em_dash():
    assert "\u2014" not in _sql(), "no em-dash anywhere"


def test_preflight_gates_are_hard():
    code = _code()
    pre = code[: code.lower().index("create table")]
    assert "raise exception" in pre.lower(), "preflight must refuse, not warn"
    assert "app_my_timers.entries_layout(timestamptz,timestamptz)" in pre
    assert "has_function_privilege('postgres'" in pre
    # carry-over 1: one emp_id per email, or an entry goes to one of two people
    assert re.search(r"from reference\.ref_employee_emails\s+group by lower\(email\)\s+"
                     r"having count\(distinct emp_id\) > 1", pre, re.I), "duplicate email gate"
    assert "pipeline.pipeline_runs" in pre and "'running'" in pre and "'failed'" in pre
    assert f"to_regclass('{TABLE}')" in pre and f"to_regclass('{PARITY_TABLE}')" in pre
    assert "nspname = 'cron'" in pre
    assert "from cron.job where jobname like 'timer-entries-clean-%'" in pre, "no job of ours exists yet"
    assert "end_time < start_time" in pre, "an entry ending before its start would fail every run"
    assert "has_table_privilege('postgres', 'app_my_timers.legacy_import', 'select')" in pre


def test_table_has_ruling_5_columns_and_pk():
    assert _table_columns(TABLE) == COLUMNS
    code = _code()
    assert re.search(r"swift_timer_id\s+text\s+primary key", code), "PK swift_timer_id"
    for idx in (r"\(work_date\)", r"\(emp_id, work_date\)", r"\(lower\(user_email\), work_date\)"):
        assert re.search(r"create index \w+ on " + re.escape(TABLE) + r"\s*" + idx, code), idx


def test_minutes_never_negative():
    code = _code()
    assert "actual_minutes       numeric(10,2) not null check (actual_minutes >= 0)" in code
    assert "check (counted_minutes >= 0 and counted_minutes <= 600)" in code


def test_mass_delete_needs_the_session_setting():
    body = _functions()[REFRESH]
    m = re.search(r"if v_gone > 10 and v_gone > v_stored \* 0\.05(.*?)then", body, re.S)
    assert m and "current_setting('timer_entries_clean.allow_mass_delete', true)" in m.group(1)
    assert "<> 'on'" in m.group(1)
    head = _header()
    assert "ALLOW A MASS DELETE" in head
    assert "set local timer_entries_clean.allow_mass_delete = 'on'; select data_staging.refresh_timer_entries_clean(" in head
    assert "allow_mass_delete" not in "".join(re.findall(r"\$cmd\$(.*?)\$cmd\$", _code(), re.S)), \
        "the cron jobs never allow a mass delete"


def test_mass_owner_loss_is_refused():
    """A reader that suddenly loses the owners (a lost grant on the email reference reads as
    empty) would rewrite the window with emp_id null: refused like a mass delete."""
    body = _functions()[REFRESH]
    assert re.search(r"s\.emp_id is not null\s+and exists \(\s*select 1 from pg_temp\._tec t\s+"
                     r"where t\.swift_timer_id = s\.swift_timer_id and t\.emp_id is null\)", body), \
        "count of stored owned rows whose new row has no owner"
    m = re.search(r"if v_lost > 10 and v_lost > v_stored \* 0\.05(.*?)then(.*?)end if;", body, re.S)
    assert m and "current_setting('timer_entries_clean.allow_mass_delete', true)" in m.group(1)
    assert "<> 'on'" in m.group(1)
    assert "raise exception" in m.group(2) and "lose their owner" in m.group(2)
    # refused before any write
    assert body.index("v_lost > 10") < body.index("insert into " + TABLE)


def test_every_function_pins_search_path_and_is_revoked():
    fns = _functions()
    assert set(fns) == {REFRESH, PARITY_FN, SNAPSHOT}, sorted(fns)
    code = _code()
    for name, stmt in fns.items():
        assert "set search_path = ''" in stmt, f"{name}: search_path = ''"
        assert "security definer" in stmt.lower(), f"{name}: runs as its owner"
        assert re.search(r"revoke all on function " + re.escape(name) + r"\([^)]*\)\s+from public, anon, authenticated", code), \
            f"{name}: revoke from public, anon, authenticated"
    refresh = fns[REFRESH]
    assert "set statement_timeout = '300s'" in refresh and "set work_mem = '64MB'" in refresh
    assert re.search(r"p_from timestamptz default now\(\) - interval '40 days'", refresh)
    assert re.search(r"p_to timestamptz default now\(\) \+ interval '1 day'", refresh)
    assert "returns jsonb" in refresh


def test_refresh_locks_first():
    body = _functions()[REFRESH]
    first = body[body.lower().index("\nbegin") + 6:].split(";")[0]
    assert "pg_advisory_xact_lock(hashtext('data_staging.refresh_timer_entries_clean'))" in first


def test_refresh_compares_every_column_but_refreshed_at():
    body = _functions()[REFRESH]
    m = re.search(r"\(([^()]*)\) is not distinct from\s*\(([^()]*)\)", body, re.S)
    assert m, "the change-only compare tuple is missing"
    c_cols = [x.strip().split(".")[1] for x in m.group(1).split(",")]
    t_cols = [x.strip().split(".")[1] for x in m.group(2).split(",")]
    assert c_cols == t_cols, "compare tuple sides differ"
    assert c_cols == [c for c in COLUMNS if c not in ("swift_timer_id", "refreshed_at")], c_cols
    s = re.search(r"do update set(.*?)returning", body, re.S | re.I).group(1)
    assert set(re.findall(r"(\w+) = excluded\.", s)) == set(COLUMNS) - {"swift_timer_id"}


def test_refresh_writes_only_differences_and_never_truncates():
    code = _code().lower()
    assert "truncate" not in code, "no truncate (readers and WAL)"
    body = _functions()[REFRESH].lower()
    assert "from app_my_timers.entries_layout(p_from, p_to)" in body
    assert "on commit drop" in body and "pg_temp." in body
    assert "on conflict (swift_timer_id) do update" in body


def test_refresh_deletes_only_inside_its_window():
    body = _functions()[REFRESH]
    m = re.search(r"delete from " + re.escape(TABLE) + r" c\s+where (.*?);", body, re.S)
    assert m, "the delete of rows that left the window is missing"
    where = m.group(1)
    assert "c.start_time >= p_from" in where and "c.start_time < p_to" in where
    assert "not exists" in where and "pg_temp._tec" in where
    assert body.count(f"delete from {TABLE}") == 1


def test_refresh_cap_and_one_now():
    body = _functions()[REFRESH]
    inner = body[body.index("$$"):]  # the defaults of the signature hold their own now()
    assert inner.count("now()") == 1, "one now() per refresh (v_now), so every row shares it"
    assert "least(m.actual_minutes, 600)" in body
    assert "m.actual_minutes > 600" in body
    # a running entry counts from its start to v_now, never below 0 (a start a moment ahead of
    # the refresh clock); a closed entry is not clamped, so one ending before it starts still fails
    assert "when l.end_time is null then greatest(v_now - l.start_time, interval '0')" in body
    assert "else l.end_time - l.start_time" in body
    assert "coalesce(l.end_time, v_now)" not in body


def test_refresh_ledger_status_values():
    body = _functions()[REFRESH]
    assert "pipeline.pipeline_runs" in body and "'timer_entries_clean'" in body
    statuses = set(re.findall(r"status\s*=?\s*'(\w+)'", body)) | set(
        re.findall(r"values \([^)]*'(running|success|failed)'", body))
    assert statuses <= {"running", "success", "failed"} and {"running", "success"} <= statuses, statuses


def test_no_legacy_function_reads_the_new_table():
    code = _code().lower()
    # The app's import ledger (a table) is read by the preflight and the parity only.
    assert "legacy_" not in code.replace("app_my_timers.legacy_import", ""), \
        "no legacy_ function is created, altered or called here"
    for name, stmt in _functions().items():
        if name != PARITY_FN:
            assert "legacy_import" not in stmt.lower(), f"{name} must not read legacy_import"
    assert "legacy_import l" not in code.replace(
        _functions()[PARITY_FN].lower(), ""), "only the parity reads the ledger rows"
    for name, stmt in _functions().items():
        if name != REFRESH and name != SNAPSHOT:
            assert f"insert into {TABLE}" not in stmt and f"update {TABLE}" not in stmt


def test_cron_commands_only_call_the_two_functions():
    code = _code()
    jobs = re.findall(r"cron\.schedule\(\s*'([\w-]+)',\s*'([^']+)',\s*\$cmd\$(.*?)\$cmd\$\s*\)", code, re.S)
    assert {(j, s) for j, s, _ in jobs} == {("timer-entries-clean-hourly", "20 * * * *"),
                                            ("timer-entries-clean-nightly", "40 19 * * *"),
                                            ("timer-entries-clean-parity", "55 19 * * *")}
    allowed = re.compile(r"^select (data_staging\.refresh_timer_entries_clean\([^;]*\)"
                         r"|data_staging\.snapshot_timer_entries_parity\(\))$")
    for job, _s, cmd in jobs:
        stmts = [s.strip() for s in cmd.split(";") if s.strip()]
        assert len(stmts) == 1, f"{job}: one call per job (a snapshot failure must not roll back a refresh)"
        for s in stmts:
            assert allowed.match(s), f"{job}: {s!r} is not a call of the two functions"
    # every job starts inactive; the ACTIVATE block turns them on after the IO diff
    wrapped = re.findall(r"cron\.alter_job\(\s*cron\.schedule\(\s*'([\w-]+)',.*?\$cmd\$\s*\),\s*active := false\s*\)",
                         code, re.S)
    assert sorted(wrapped) == sorted(j for j, _s, _c in jobs), wrapped
    head = _header()
    assert "ACTIVATE" in head and "pg_stat_statements" in head
    assert ("select cron.alter_job(jobid, active := true) from cron.job "
            "where jobname like 'timer-entries-clean-%';") in head
    cmds = dict((j, c) for j, _s, c in jobs)
    assert "2025-08-31 16:00:00+00" in cmds["timer-entries-clean-nightly"]
    assert cmds["timer-entries-clean-parity"] == "select data_staging.snapshot_timer_entries_parity()"
    snap = _functions()[SNAPSHOT]
    assert "pg_advisory_xact_lock(hashtext('data_staging.refresh_timer_entries_clean'))" in snap, \
        "the snapshot waits for a refresh still running"



def test_parity_keys_and_causes():
    stmt = _functions()[PARITY_FN]
    assert re.search(r"returns table \(emp_id text, user_email text, day date, new_minutes numeric, "
                     r"old_minutes numeric, cause text\)", stmt)
    for cause in ("'cap'", "'running'", "'my_timers_fix'", "'unowned_in_my_timers'",
                  "'v1_fix_not_carried'", "'only_in_my_timers'", "'only_in_v1'", "'unexplained'"):
        assert cause in stmt, cause
    assert "data_staging.stg_timer_activities_clean" in stmt and TABLE in stmt
    assert "reference.ref_employee_emails" in stmt, "v1 emails map to emp_id"
    assert "> 1" in stmt and "Asia/Manila" in stmt
    # a moved start explains Swift's original day too, not only the removals
    fixed = re.search(r"fixed_days as \((.*?)\n  \),", stmt, re.S).group(1)
    assert "c.last_kind = 'remove'" in fixed and "c.new_start is not null" in fixed
    assert "(c.start_time at time zone 'Asia/Manila')::date" in fixed
    order = ["'cap'", "'running'", "'my_timers_fix'", "'unowned_in_my_timers'",
             "'v1_fix_not_carried'", "'only_in_my_timers'", "'unexplained'"]
    assert [stmt.index(c) for c in order] == sorted(stmt.index(c) for c in order), "cause order"
    # an old-system fix the app's importer could not carry explains the day it is on
    nc = re.search(r"not_carried as \((.*?)\n  \),", stmt, re.S)
    assert nc, "not_carried CTE"
    nc = nc.group(1)
    assert "app_my_timers.legacy_import" in nc
    assert "distinct on (l.source, l.source_id)" in nc and "l.source_version desc" in nc, "latest version"
    assert "('no_entry', 'ambiguous', 'no_member', 'no_task', 'own_addition')" in nc
    assert "(l.old_row ->> 'start_time')::timestamptz at time zone 'Asia/Manila'" in nc
    # the same null-project filter on both sides
    new_rows = re.search(r"new_rows as \((.*?)\n  \),", stmt, re.S).group(1)
    assert "n.project_did is not null" in new_rows
    old_days = re.search(r"old_days as \((.*?)\n  \),", stmt, re.S).group(1)
    assert "c.project_did is not null" in old_days
    assert _table_columns(PARITY_TABLE)[:7] == ["emp_id", "user_email", "day", "new_minutes",
                                                "old_minutes", "cause", "checked_at"]


def test_rls_grants_and_metadata():
    code = _code()
    for t in (TABLE, PARITY_TABLE):
        assert f"alter table {t} enable row level security" in code, t
        assert f"revoke all on {t} from anon, authenticated" in code, t
        assert not re.search(r"create policy \S+ on " + re.escape(t), code), f"{t}: no policy"
        # read only for service_role: everything revoked, then select alone
        rv = code.index(f"revoke all on {t} from service_role;")
        assert rv < code.index(f"grant select on {t} to service_role;"), t
    head = _header()
    assert "has_table_privilege('my_timers_owner', 'data_staging.stg_timer_entries_clean', 'select')" in head
    meta = code[code.index("insert into agent.schema_metadata"):]
    assert "Group by emp_id, else lower(user_email)" in meta, "unowned entries carry emp_id null"
    assert "unowned_in_my_timers" in meta and "block the D13 cutover" in meta
    assert "PII" in meta and "Retention" in meta and "stg_timer_activities_clean" in meta
    assert "'stg_timer_entries_clean'" in meta and "'stg_timer_entries_parity'" in meta
    assert "array[]::text[]" in meta or "::text[]" in meta
    assert "where not exists" in meta and "is not distinct from v.column_name" in meta
    for c in COLUMNS:
        assert f"'{c}'" in meta, f"column row for {c}"
