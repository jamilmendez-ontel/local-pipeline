"""Unit tests for duplicate_asset_watcher's pure helpers (no network, no DB).
Run: cd swift_api_pipeline && python -m pytest test_duplicate_asset_watcher.py -v
"""

from datetime import datetime, timezone

from duplicate_asset_watcher import (
    push_created_at, group_rows, classify_group, select_groups, is_full_list_day,
    build_email_body, NEW_HOURS,
)

KEEP = "-P3sPO9nXQmaGuy_lLm8"    # created 2026-10-14 03:16:45 UTC, 5 approved tasks, 2 timers
DUP = "-P3sPXeBfpUeNzcPFNhW"     # created 39 s later, all 76 tasks pending, no timers


def _row(did, touched=0, timers=0, name="WINCHESTER - 5G L-SUB6 - CARRIER ADD",
         project="TECH-OPS: TS20", path="MRC Towers/VZW/Mountain Plains-KS/Embedded/17433804/Oct 2026"):
    return {"project_name": project, "project_did": "-Ozh7xx_YHk8yXEmVmFR", "asset_id": path,
            "asset_name": name, "asset_did": did, "task_count": 76, "touched": touched,
            "timers": timers}


def test_push_created_at_decodes_the_swift_id_timestamp():
    assert push_created_at(KEEP) == datetime(2026, 10, 14, 3, 16, 45, 363000, tzinfo=timezone.utc)
    assert push_created_at(DUP) == datetime(2026, 10, 14, 3, 17, 24, 236000, tzinfo=timezone.utc)


def test_push_created_at_returns_none_for_a_non_push_id():
    assert push_created_at("") is None
    assert push_created_at("not a push id!") is None


def test_group_rows_groups_by_project_path_and_name():
    rows = [_row(KEEP, touched=13, timers=2), _row(DUP),
            _row("-Pa", name="OTHER"), _row("-Pb", name="OTHER")]
    groups = group_rows(rows)
    assert len(groups) == 2
    assert {a["asset_did"] for a in groups[0]["assets"]} == {KEEP, DUP}


def test_one_active_copy_is_kept_and_the_empty_ones_are_flagged():
    g = classify_group([_row(KEEP, touched=13, timers=2), _row(DUP)])
    assert g["verdict"] == "delete_empty"
    assert g["keep"] == KEEP and g["remove"] == [DUP]


def test_all_empty_copies_keep_the_oldest():
    g = classify_group([_row(DUP), _row(KEEP)])
    assert g["verdict"] == "all_empty"
    assert g["keep"] == KEEP and g["remove"] == [DUP]


def test_two_active_copies_need_a_merge_and_nothing_is_flagged_for_removal():
    g = classify_group([_row(KEEP, touched=5), _row(DUP, timers=1)])
    assert g["verdict"] == "merge_needed"
    assert g["keep"] is None and g["remove"] == []


def test_daily_run_reports_only_groups_with_a_copy_created_recently():
    old = classify_group([_row(KEEP, touched=1), _row(DUP)])
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    assert select_groups([old], now, full=False) == [old]
    later = datetime(2026, 10, 14, 3, 17, 25, tzinfo=timezone.utc).replace(day=16)
    assert (later - push_created_at(DUP)).total_seconds() > NEW_HOURS * 3600
    assert select_groups([old], later, full=False) == []
    assert select_groups([old], later, full=True) == [old]


def test_full_list_goes_out_on_monday_eastern_time():
    # 2026-10-19 is a Monday; 03:00 UTC is still Sunday evening ET
    assert is_full_list_day(datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc))
    assert not is_full_list_day(datetime(2026, 10, 19, 3, 0, tzinfo=timezone.utc))
    assert not is_full_list_day(datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))


def test_email_body_names_the_copy_to_keep_and_the_one_to_delete():
    g = classify_group([_row(KEEP, touched=13, timers=2), _row(DUP)])
    body = build_email_body([g], datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc), full=True, total=1)
    assert "TECH-OPS: TS20" in body and "WINCHESTER - 5G L-SUB6 - CARRIER ADD" in body
    keep_line = next(l for l in body.splitlines() if KEEP in l)
    dup_line = next(l for l in body.splitlines() if DUP in l)
    assert "KEEP" in keep_line and "13 tasks touched" in keep_line and "2 timers" in keep_line
    assert "DELETE" in dup_line
    assert "—" not in body   # house style: no em dash in copy


def test_email_body_for_a_merge_case_says_so():
    g = classify_group([_row(KEEP, touched=5), _row(DUP, timers=1)])
    body = build_email_body([g], datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc), full=False, total=1)
    assert "MERGE" in body


def test_a_copy_with_unknown_task_state_is_never_flagged_for_deletion():
    g = classify_group([_row(KEEP, touched=13, timers=2), _row(DUP, touched=None)])
    assert g["verdict"] == "merge_needed" and g["remove"] == []
    body = build_email_body([g], datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc), full=True, total=1)
    assert "task state unknown" in body and "None" not in body


def test_timer_counts_are_aggregated_once_not_per_asset():
    from duplicate_asset_watcher import DUPLICATES_SQL
    assert "tc AS (" in DUPLICATES_SQL and "LEFT JOIN tc ON tc.asset_did = s.asset_did" in DUPLICATES_SQL
    assert "(SELECT count(*)" not in DUPLICATES_SQL
