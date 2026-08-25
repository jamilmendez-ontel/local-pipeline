"""Render sample Timer Activity Entries emails (daily + resend) with a
running timer, WITHOUT touching Gmail or the DB. Writes HTML files to
swift_api_pipeline/out/ for eyeballing.

Run: python tests/render_running_entries_sample.py
"""
import base64
import email
import os
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import timer_correction_review as tcr  # noqa: E402
import gmail_client  # noqa: E402

OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)

SENT = []


class _Exec:
    def __init__(self, payload):
        self._p = payload

    def execute(self):
        return self._p


class _Messages:
    def send(self, userId, body):
        SENT.append(body["raw"])
        return _Exec({"threadId": "thread-1", "id": "msg-1"})

    def get(self, userId, id, format=None, metadataHeaders=None):
        return _Exec({"payload": {"headers": [{"name": "Message-ID", "value": "<x@y>"}]}})


class _Users:
    def messages(self):
        return _Messages()


class _Service:
    def users(self):
        return _Users()


class _FakeDb:
    async def execute(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []


gmail_client.authenticate = lambda: _Service()
gmail_client.masked_sender = lambda service, name: f"{name} <timer@ontel.co>"
tcr.retry_db = lambda fn, description="": None
# Pretend the IVORY task resolved to a Swift task DID; the admin timer has no asset.
tcr._lookup_task_dids = lambda db, entries: {
    k: "-OwTPtaskDidSample" for k in (tcr._task_link_key(e) for e in entries) if k}

T = datetime(2026, 8, 23, 13, 40, 39, tzinfo=timezone.utc)
U = "prince@ontel.co"


def e(start, minutes, task, site, site_id, end="auto"):
    if end == "auto":
        end = start + timedelta(minutes=minutes)
    return {
        "project": "TECH-OPS: TS19", "project_did": "-OmzvGwfYsSskngv6SEo",
        "user_email": U, "start_time": start, "end_time": end,
        "duration_min": minutes if end is not None else 0,
        "site_name": site, "site_id": site_id, "task": task,
        "task_clean": task.split(". ", 1)[-1], "asset_did": "-OvptiE2IlrkuMb8GUA7",
    }


entries = [
    e(T, 131, "4. 48Hr / Test Package Complete", "PORT OF RICHMOND - A - New Build", "SBA/VZW/SOVA/17634574"),
    e(T + timedelta(hours=3), 48, "2. Live Review Complete", "GOODSPUR - B - 5G L-SUB6 - CARRIER ADD", "VZW/CGC/17391884"),
    e(T + timedelta(hours=4, minutes=21), 0, "6. Final COP Complete", "IVORY-HGCW-TX - RADIO SWAP", "VZW/CGC/17077741", end=None),
    e(T + timedelta(hours=6), 0, "1. General Admin and Support", None, None, end=None),
]


def _html_of(raw):
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode()
    raise RuntimeError("no html part")


tcr.send_daily_emails(_FakeDb(), entries, test_mode=True, target_date=date(2026, 8, 23))
open(os.path.join(OUT, "sample_daily_running.html"), "w", encoding="utf-8").write(_html_of(SENT[-1]))

# Resend: the IVORY timer completed (NEW), the admin one still running.
resend_entries = [dict(x) for x in entries[:2]]
done = e(T + timedelta(hours=4, minutes=21), 95, "6. Final COP Complete", "IVORY-HGCW-TX - RADIO SWAP", "VZW/CGC/17077741")
resend_entries.append(done)
resend_entries.append(entries[3])
for x in resend_entries:
    x["is_edited"] = False
snapshot = set(tcr._collect_entry_ids(entries[:2]))
tcr.find_days_needing_resend = lambda db, lookback_days=7: [{
    "user_email": U, "send_date": date(2026, 8, 23), "thread_id": "thread-1",
    "message_id": "<x@y>", "snapshot_ids": snapshot, "current_entries": resend_entries,
}]
tcr.send_resend_emails(_FakeDb(), test_mode=True)
open(os.path.join(OUT, "sample_resend_running.html"), "w", encoding="utf-8").write(_html_of(SENT[-1]))

# Only-running day.
SENT.clear()
tcr.send_daily_emails(_FakeDb(), [entries[3]], test_mode=True, target_date=date(2026, 8, 23))
open(os.path.join(OUT, "sample_daily_only_running.html"), "w", encoding="utf-8").write(_html_of(SENT[-1]))
print("wrote", OUT)
