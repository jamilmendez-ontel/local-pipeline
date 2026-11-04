"""A rejected AR Aging attachment is recorded once, loads nothing, and turns the run red."""

from datetime import datetime, timezone

import pandas as pd

import extract_aging
import main
from pipeline_notifier import PipelineOutcome

RECEIVED = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def _bad_file(path):
    heads = ["Date", "Transaction Type", "Num", "Customer", "Location", "Due Date",
             "Amount", "Open Balance", "Client/Vendor Message", "P.O. Number"]
    blank = [None] * (len(heads) + 1)
    sheet = [["ONTEL LLC"] + blank[1:], ["A/R Aging Detail"] + blank[1:],
             ["As of October 30, 2026"] + blank[1:], list(blank), [None] + heads,
             ["Current"] + blank[1:],
             [None, "09/15/2026", "Invoice", "65154", "Betacom", "GA", "10/16/2026",
              585, 585, "124629", "Requested 09/16/2026 (SM)"]]
    pd.DataFrame(sheet).to_excel(path, header=False, index=False)


class FakeDB:
    def __init__(self, rejected=()):
        self.rejected = set(rejected)
        self.executed = []

    def fetch(self, sql, *args):
        if "raw_ar_aging" in sql:
            return []
        if "pipeline_runs" in sql:
            return [{"ts": t} for t in self.rejected]
        raise AssertionError(sql)

    def execute(self, sql, *args):
        self.executed.append((sql, args))

    def executemany(self, sql, rows):
        self.executed.append((sql, rows))


def _patch(monkeypatch, tmp_path, db):
    monkeypatch.setattr(extract_aging, "get_db", lambda: db)
    monkeypatch.setattr(extract_aging, "retry_db", lambda fn, description=None: fn())
    monkeypatch.setattr(extract_aging, "authenticate", lambda: object())
    monkeypatch.setattr(extract_aging, "search_messages", lambda svc, q, max_results=100: [{"id": "m1"}])
    monkeypatch.setattr(extract_aging, "get_message_details",
                        lambda svc, mid: {"id": mid, "received_date": RECEIVED,
                                          "subject": "Re: Daily Revenue Report"})
    downloads = []

    def fake_download(svc, mid, pattern, tmp_dir):
        p = tmp_path / "ONTEL+LLC_AR+Aging+Detail As of 10.30.2026.xlsx"
        _bad_file(p)
        downloads.append(mid)
        return str(p)

    monkeypatch.setattr(extract_aging, "download_attachment", fake_download)
    return downloads


def test_bad_layout_is_recorded_and_nothing_loaded(monkeypatch, tmp_path):
    db = FakeDB()
    _patch(monkeypatch, tmp_path, db)
    result = extract_aging.run_aging_pipeline()

    assert result["processed"] == []
    assert len(result["parse_failures"]) == 1
    assert "Past Due" in result["parse_failures"][0]
    sqls = " ".join(s for s, _ in db.executed)
    assert "raw_ar_aging" not in sqls and "stg_ar_aging" not in sqls
    inserts = [a for s, a in db.executed if s.startswith("INSERT INTO") and "pipeline_runs" in s]
    assert len(inserts) == 1
    meta = inserts[0][4]
    assert meta["failure"] == "parse"
    assert meta["email_received_date"] == RECEIVED.isoformat()
    updates = [a for s, a in db.executed if s.startswith("UPDATE") and "pipeline_runs" in s]
    assert updates[0][0] == "failed"


def test_rejected_email_is_not_retried(monkeypatch, tmp_path):
    db = FakeDB(rejected={RECEIVED.strftime("%Y-%m-%d %H:%M:%S")})
    downloads = _patch(monkeypatch, tmp_path, db)
    result = extract_aging.run_aging_pipeline()
    assert downloads == []
    assert result["parse_failures"] == []
    assert db.executed == []


def test_full_run_reports_partial_failure(monkeypatch):
    import extract_aging as ea
    monkeypatch.setattr(ea, "run_aging_pipeline",
                        lambda: {"processed": [], "parse_failures": ["x.xlsx: missing Past Due"]})
    out = main.run_aging_pipeline_full()
    assert isinstance(out, PipelineOutcome)
    assert out.email_status() == "PARTIAL FAILURE"
    assert "missing Past Due" in out.detail

    monkeypatch.setattr(ea, "run_aging_pipeline", lambda: {"processed": ["d"], "parse_failures": []})
    assert main.run_aging_pipeline_full() is True
