# swift_api_pipeline/tests/test_onpoint_daily_reports.py
"""Offline tests for run_onpoint_daily_reports.py (no network, no DB)."""
import os
import pathlib
import sys
from datetime import date

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import run_onpoint_daily_reports as m  # noqa: E402


def rep(i, d="2026-11-18", st="submitted", **kw):
    base = {"id": f"00000000-0000-0000-0000-{i:012d}", "user_id": "11111111-1111-1111-1111-111111111111",
            "report_date": d, "status": st, "self_reported_hours": 9, "owner_name": "X"}
    base.update(kw)
    return base


class FakeResp:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._body, self.text = code, body, text

    def json(self):
        return self._body


class FakeSession:
    """Answers time.approvals.list from a {(date, status): rows} map; logs calls."""

    def __init__(self, by_key, codes=None):
        self.by_key, self.codes, self.calls = by_key, list(codes or []), []

    def get(self, url, params, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        if self.codes:
            return FakeResp(self.codes.pop(0), text="boom")
        return FakeResp(200, {"data": self.by_key.get((params["from"], params["status"]), [])})


def test_window_trailing_days_plus_one():
    assert m.window(date(2026, 11, 20), 14, None) == (date(2026, 11, 7), date(2026, 11, 21))
    assert m.window(date(2026, 11, 20), 1, None) == (date(2026, 11, 20), date(2026, 11, 21))


def test_window_since_overrides_days_and_rejects_future_start():
    assert m.window(date(2026, 11, 20), 14, "2026-11-01")[0] == date(2026, 11, 1)
    with pytest.raises(ValueError):
        m.window(date(2026, 11, 20), 14, "2026-12-30")


def test_normalize_keeps_only_landed_fields():
    n = m.normalize(rep(1, extra="ignored"))
    assert "extra" not in n and n["status"] == "submitted" and set(n) == set(m.REPORT_FIELDS)


@pytest.mark.parametrize("bad", [{"id": None}, {"status": "in_progress"}, {"report_date": "nope"}])
def test_normalize_rejects_bad_rows(bad):
    with pytest.raises((m.OnPointError, ValueError)):
        m.normalize(rep(1, **bad))


def test_pull_asks_one_day_and_status_per_call_and_dedups():
    s = FakeSession({("2026-11-18", "submitted"): [rep(1)],
                     ("2026-11-19", "approved"): [rep(2, d="2026-11-19", st="approved")]})
    out = m.pull_reports(s, date(2026, 11, 18), date(2026, 11, 19))
    assert sorted(r["id"][-1] for r in out) == ["1", "2"]
    assert len(s.calls) == 2 * len(m.STATUSES)
    assert all(p["from"] == p["to"] and p["limit"] == m.PAGE_CAP for _, p in s.calls)


def test_pull_refuses_a_full_page():
    rows = [rep(i) for i in range(m.PAGE_CAP)]
    with pytest.raises(m.OnPointError, match="cut off"):
        m.pull_reports(FakeSession({("2026-11-18", "submitted"): rows}), date(2026, 11, 18), date(2026, 11, 18))


def test_pull_refuses_a_row_for_another_day_or_status():
    s = FakeSession({("2026-11-18", "submitted"): [rep(1, st="approved")]})
    with pytest.raises(m.OnPointError, match="came back"):
        m.pull_reports(s, date(2026, 11, 18), date(2026, 11, 18))


def test_call_retries_5xx_then_raises_on_4xx(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    s = FakeSession({}, codes=[503])
    assert m.call(s, "time.approvals.list", {"from": "2026-11-18", "status": "submitted"}) == {"data": []}
    with pytest.raises(m.OnPointError, match="HTTP 403"):
        m.call(FakeSession({}, codes=[403]), "time.approvals.list", {"from": "x", "status": "y"})


def test_empty_window_never_loads(monkeypatch):
    monkeypatch.setenv("ONPOINT_PAT", "x")
    monkeypatch.setattr(m, "pull_reports", lambda *a: [])
    with pytest.raises(m.OnPointError, match="refusing to load"):
        m.run(14, None, dry_run=False)


def test_missing_pat(monkeypatch):
    monkeypatch.delenv("ONPOINT_PAT", raising=False)
    with pytest.raises(m.OnPointError, match="ONPOINT_PAT"):
        m.run(14, None, dry_run=True)


def test_load_sql_deletes_only_unlisted_person_days_inside_the_window_under_a_cap():
    sql = m.LOAD_SQL
    assert "s.report_date between $2::date and $3::date" in sql
    # a person-day still listed is never a delete candidate (it is upserted instead)
    assert "not exists (select 1 from src where src.user_id = s.user_id and src.report_date = s.report_date)" in sql
    assert "and (select allowed from del_ok)" in sql
    assert "where r.report_id in (select report_id from stg_del)" in sql


def test_load_sql_upserts_on_the_person_day_so_a_refiled_report_replaces_the_old_id():
    assert "on conflict (user_id, report_date) do update set\n    report_id = excluded.report_id" in m.LOAD_SQL
    assert "round(worked_minutes)::integer" in m.LOAD_SQL


def test_migration_294_keeps_onpoint_rows_out_of_the_swift_approve_queue():
    mig = (pathlib.Path(HERE).parent / "migrations" / "294_drmc_reads_onpoint_daily_reports.sql").read_text(encoding="utf-8")
    assert mig.count("op.emp_id IS NULL AND t.task_status = 'submitted'::text AND t.approved_on IS NULL") == 3
    assert "DROP VIEW analytics.v_hr_report_review" not in mig
    assert "CREATE OR REPLACE VIEW analytics.v_hr_report_review" in mig
    assert "(r.self_reported_hours IS NOT NULL OR r.submitted_at IS NOT NULL)" in mig
