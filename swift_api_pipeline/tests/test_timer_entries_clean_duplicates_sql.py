# swift_api_pipeline/tests/test_timer_entries_clean_duplicates_sql.py
"""Contract tests for migration 286 (an exact duplicate Swift entry counts once).

Jamil decided 2026-11-02 ("counting once"): within one owner, start_time, project_did,
task_did and asset_did, the copy with the most actual minutes counts (tie: lowest
swift_timer_id); every other copy keeps its row with is_duplicate = true and
counted_minutes = 0. My Timers keeps showing both copies; only the warehouse count changes.

These tests pin, on the migration text:
  1. the house header (purpose, STATUS: NOT APPLIED, WHY, PREFLIGHT, ROLLBACK, VERIFY);
  2. the column is added with a constant default (no table rewrite);
  3. the refresh is the 285 body plus the duplicate mark only (same signature, settings,
     revokes), and is_duplicate is in the insert list, the compare tuple and the update;
  4. a duplicate counts 0 and never touches is_capped; null project rows are never grouped;
  5. the parity has cause 'duplicate' just before 'unexplained' and leaves duplicates out of
     the uncapped sum;
  6. schema_metadata gets the column row and the table text; no truncate; 285 unchanged.
"""
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
MIGS = os.path.join(os.path.dirname(HERE), "migrations")
MIG = os.path.join(MIGS, "286_stg_timer_entries_clean_duplicates.sql")
MIG_285 = os.path.join(MIGS, "285_stg_timer_entries_clean.sql")

TABLE = "data_staging.stg_timer_entries_clean"
REFRESH = "data_staging.refresh_timer_entries_clean"
PARITY_FN = "data_staging.timer_entries_parity"

COLUMNS_285 = ["swift_timer_id", "emp_id", "user_email", "project_did", "project_number",
               "asset_did", "site_name", "task_did", "task_clean", "task_name", "start_time",
               "end_time", "work_date", "actual_minutes", "counted_minutes", "is_running",
               "is_capped", "source", "edited_fields", "fixed_by_super_admin", "refreshed_at"]
COLUMNS = COLUMNS_285 + ["is_duplicate"]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _header(sql):
    return sql[: sql.index("-- ====", 10) + 4] if sql.startswith("-- ====") else ""


def _code(sql):
    """The SQL with every -- comment removed (strings here never hold '--')."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _functions(sql):
    code = _code(sql)
    out = {}
    for m in re.finditer(r"create (?:or replace )?function ([\w.]+)\s*\(", code, re.I):
        body_start = code.index("$$", m.end())
        body_end = code.index("$$", body_start + 2)
        out[m.group(1).lower()] = code[m.start(): body_end + 2]
    return out


def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def test_header_blocks_present():
    head = _header(_read(MIG))
    assert head, "the file must open with the house header block"
    for block in ("286_stg_timer_entries_clean_duplicates.sql", "STATUS: APPLIED", "WHY",
                  "PREFLIGHT", "ROLLBACK", "VERIFY"):
        assert block in head, f"header lacks {block}"
    for step in ("alter table data_staging.stg_timer_entries_clean drop column if exists is_duplicate",
                 "delete from agent.schema_metadata",
                 "285_stg_timer_entries_clean.sql"):
        assert step in head, f"ROLLBACK lacks {step}"
    assert "select data_staging.snapshot_timer_entries_parity();" in head
    assert "'unexplained'" in head or "unexplained" in head


def test_no_em_dash():
    assert "—" not in _read(MIG), "no em-dash anywhere"


def test_column_added_with_constant_default():
    code = _code(_read(MIG))
    assert re.search(r"alter table " + re.escape(TABLE) +
                     r"\s+add column is_duplicate boolean not null default false;", code)


def test_preflight_is_hard():
    code = _code(_read(MIG))
    pre = code[: code.lower().index("alter table")]
    assert "raise exception" in pre.lower()
    assert f"to_regclass('{TABLE}')" in pre
    assert "is_duplicate" in pre, "the column must not exist yet"


def test_only_the_two_functions_are_replaced_with_285_signatures():
    fns = _functions(_read(MIG))
    assert set(fns) == {REFRESH, PARITY_FN}, sorted(fns)
    old = _functions(_read(MIG_285))
    code = _code(_read(MIG))
    for name, stmt in fns.items():
        assert stmt.lower().startswith("create or replace function"), name
        sig = lambda s: _norm(s[: s.index("$$")])
        assert sig(stmt).replace("create or replace function", "create function") == sig(old[name]), \
            f"{name}: same signature, language and settings as 285"
        assert re.search(r"revoke all on function " + re.escape(name) + r"\([^)]*\)\s+from public, anon, authenticated", code)


def test_refresh_is_the_285_body_plus_the_duplicate_mark():
    new = _functions(_read(MIG))[REFRESH]
    old = _functions(_read(MIG_285))[REFRESH]
    stripped = re.sub(r"update pg_temp\._tec t\s+set is_duplicate = true.*?and d\.rn > 1;", "", new, flags=re.S)
    assert stripped != new, "the duplicate mark is missing"
    stripped = re.sub(r"is_duplicate = excluded\.is_duplicate,\s*", "", stripped)
    stripped = re.sub(r",\s*(?:[ct]\.)?is_duplicate\b", "", stripped)
    assert "is_duplicate" not in stripped
    assert _norm(stripped).replace("create or replace function", "create function") == _norm(old)


def test_refresh_writes_is_duplicate_everywhere():
    body = _functions(_read(MIG))[REFRESH]
    m = re.search(r"\(([^()]*)\) is not distinct from\s*\(([^()]*)\)", body, re.S)
    c_cols = [x.strip().split(".")[1] for x in m.group(1).split(",")]
    t_cols = [x.strip().split(".")[1] for x in m.group(2).split(",")]
    assert c_cols == t_cols
    assert c_cols == [c for c in COLUMNS if c not in ("swift_timer_id", "refreshed_at")], c_cols
    s = re.search(r"do update set(.*?)returning", body, re.S | re.I).group(1)
    assert set(re.findall(r"(\w+) = excluded\.", s)) == set(COLUMNS) - {"swift_timer_id"}
    ins = re.search(r"insert into " + re.escape(TABLE) + r" as s \((.*?)\)", body, re.S).group(1)
    assert [x.strip() for x in ins.split(",")] == COLUMNS
    sel = re.search(r"select (t\.swift_timer_id.*?)\s+from pg_temp\._tec t", body, re.S).group(1)
    assert [x.strip().split(".")[1] for x in sel.split(",")] == COLUMNS


def test_duplicate_mark_counts_zero_and_keeps_the_longest():
    body = _functions(_read(MIG))[REFRESH]
    m = re.search(r"update pg_temp\._tec t\s+set is_duplicate = true(.*?)and d\.rn > 1;", body, re.S)
    assert m, "the mark"
    mark = m.group(0)
    assert re.search(r"set is_duplicate = true,\s*counted_minutes = 0\s", mark)
    assert "is_capped" not in mark, "is_capped stays as computed from actual_minutes"
    assert re.search(r"partition by coalesce\(x\.emp_id, 'email:' \|\| lower\(x\.user_email\)\),\s*"
                     r"x\.start_time, x\.project_did, x\.task_did, x\.asset_did", mark)
    # a closed copy counts before a running one (v1 counts a running timer as 0), then the longest
    assert re.search(r"order by \(x\.end_time is null\), x\.actual_minutes desc, x\.swift_timer_id\)", mark)
    assert "where x.project_did is not null and x.source = 'swift'" in mark, \
        "only Swift rows are grouped (a legacy row is v1's own row; a request is never a duplicate); no project, never grouped"
    assert "and (x.emp_id is not null or x.user_email is not null)" in mark, \
        "a row with neither owner nor email is never grouped (they would share one partition)"
    # after the one-row-per-id check, before the guards and any write
    assert body.index("create unique index on pg_temp._tec") < body.index(mark) < body.index("v_lost > 10")
    assert body.index(mark) < body.index("insert into " + TABLE)


def test_only_swift_rows_are_marked_and_documented():
    sql = _read(MIG)
    head = _header(sql)
    assert "source = 'swift'" in head and "121 legacy copies" in head
    assert "about 1,053 on production" in head, "VERIFY names the expected nightly updates"
    assert "a closed copy counts before a running one" in head
    assert "begin; set local lock_timeout = '10s';" in head[head.index("ROLLBACK"):head.index("VERIFY")]
    meta = _code(sql)[_code(sql).index("insert into agent.schema_metadata"):]
    assert "Only source swift rows are marked" in meta and "source swift only" in meta


def test_parity_duplicate_cause():
    stmt = _functions(_read(MIG))[PARITY_FN]
    order = ["'cap'", "'running'", "'my_timers_fix'", "'unowned_in_my_timers'",
             "'v1_fix_not_carried'", "'only_in_my_timers'", "'only_in_v1'", "'duplicate'",
             "'unexplained'"]
    for c in order:
        assert c in stmt, c
    assert [stmt.index(c) for c in order] == sorted(stmt.index(c) for c in order), "cause order"
    assert "when d.duplicated then 'duplicate'" in stmt
    assert "bool_or(r.is_duplicate) as duplicated" in stmt
    # the cap check reads the uncapped minutes of the copies that count only
    assert "sum(r.actual_minutes) filter (where not r.is_duplicate) as uncapped" in stmt
    assert "bool_or(r.is_capped) filter (where not r.is_duplicate) as capped" in stmt
    new_rows = re.search(r"new_rows as \((.*?)\n  \),", stmt, re.S).group(1)
    assert "n.is_duplicate" in new_rows and "n.project_did is not null" in new_rows


def test_metadata_column_row_and_table_text():
    code = _code(_read(MIG))
    meta = code[code.index("insert into agent.schema_metadata"):]
    assert "'stg_timer_entries_clean', 'is_duplicate'" in meta
    assert "where not exists" in meta and "is not distinct from v.column_name" in meta
    assert re.search(r"update agent\.schema_metadata\s+set data_notes = .*?counted once.*?"
                     r"where schema_name = 'data_staging' and table_name = 'stg_timer_entries_clean'\s+"
                     r"and column_name is null;", code, re.S)
    assert re.search(r"update agent\.schema_metadata.*?only_in_v1, duplicate, unexplained.*?table_name = 'stg_timer_entries_parity'",
                     code, re.S), "the parity cause list names duplicate"


def test_no_truncate_and_one_transaction():
    code = _code(_read(MIG)).lower()
    assert "truncate" not in code
    assert code.strip().startswith("begin;") and code.strip().endswith("commit;")
    assert "cron.schedule" not in code, "the 285 jobs keep calling the same functions"


def test_285_file_unchanged_vs_head():
    root = os.path.dirname(os.path.dirname(HERE))
    r = subprocess.run(["git", "diff", "--quiet", "HEAD", "--",
                        "swift_api_pipeline/migrations/285_stg_timer_entries_clean.sql"], cwd=root)
    assert r.returncode == 0, "285 is applied on production: its file never changes"
