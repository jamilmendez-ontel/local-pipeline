# swift_api_pipeline/tests/test_inc_upsert_guards.py
"""Contract tests for the shadow walker's guarded upserts.

The guards exist so unchanged rows are skipped, but a guard that omits a
column the SET clause writes silently discards real changes forever: the
2026-08-06 drift root-cause was UPSERT_ASSET's guard tuple missing
asset_id, so renames that changed only the asset_id path never landed in
stg_assets_inc (and SYNC_TASK_ATTRS then propagated the stale value).
These tests pin the invariant: every column a guard's SET clause writes
(except loaded_at, which intentionally moves on every real update) must
appear in its IS DISTINCT FROM comparison tuple.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from extract_asset_tasks_inc import UPSERT_ASSET, UPSERT_TASK


def set_columns(sql):
    """Columns written by the ON CONFLICT ... DO UPDATE SET clause."""
    set_clause = re.search(r"DO UPDATE SET(.*?)WHERE", sql, re.S).group(1)
    return set(re.findall(r"(\w+)\s*=\s*EXCLUDED\.", set_clause))


def guard_columns(sql, table):
    """Columns compared in the WHERE (...) IS DISTINCT FROM (...) guard."""
    where_clause = sql[sql.index("WHERE"):]
    return set(re.findall(rf"{table}\.(\w+)", where_clause))


def test_asset_upsert_guard_compares_every_written_column():
    written = set_columns(UPSERT_ASSET) - {"loaded_at"}
    guarded = guard_columns(UPSERT_ASSET, "stg_assets_inc")
    assert written == guarded, (
        f"UPSERT_ASSET guard skips changes to {sorted(written - guarded)}: "
        "a row whose only change is in those columns is never updated"
    )


def test_task_upsert_guard_compares_every_written_column():
    written = set_columns(UPSERT_TASK) - {"loaded_at"}
    guarded = guard_columns(UPSERT_TASK, "stg_asset_tasks_inc")
    assert written == guarded, (
        f"UPSERT_TASK guard skips changes to {sorted(written - guarded)}"
    )
