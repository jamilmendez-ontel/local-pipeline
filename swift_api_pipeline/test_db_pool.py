"""Tests for the DB pool startup invariant. Run:
    cd swift_api_pipeline && python -m pytest test_db_pool.py -v

These monkeypatch _create_pool, so NO real database connection is made."""
import asyncio
import threading
import time

import pytest

import db as dbmod
from db import PipelineDB


def test_start_blocks_until_pool_ready_even_when_thread_already_alive(monkeypatch):
    """Regression for the GHA 'NoneType object has no attribute acquire' failure.

    start() used to early-return whenever a background thread was already alive,
    even if that thread had not finished creating the pool. After a slow first
    connect tripped the ready timeout, reconnect() -> start() returned on the
    still-alive thread with _pool is None, and callers then ran queries against
    a None pool. start() must wait for the POOL, not merely for a live thread."""
    gate = threading.Event()

    class FakePool:
        async def close(self):  # for clean teardown via inst.close()
            return None

    async def fake_create_pool(self):
        # Cooperatively block until the test releases the gate, then publish a
        # sentinel pool. Mimics a slow connect that completes after start().
        while not gate.is_set():
            await asyncio.sleep(0.01)
        self._pool = FakePool()

    monkeypatch.setattr(PipelineDB, "_create_pool", fake_create_pool)
    monkeypatch.setattr(dbmod, "POOL_READY_TIMEOUT", 5)

    inst = PipelineDB()
    # Spin up the loop thread directly so a thread is "already alive" while the
    # pool is still None and _ready is unset (the exact broken window).
    inst._thread = threading.Thread(target=inst._run_loop, daemon=True, name="db-loop")
    inst._thread.start()
    while inst._loop is None or not inst._loop.is_running():
        time.sleep(0.001)
    assert inst._pool is None

    # Release the gate shortly AFTER calling start() so the pool only becomes
    # ready mid-wait. Buggy start() returns immediately (pool None) and fails the
    # assertion; fixed start() blocks on _ready until the pool exists.
    threading.Timer(0.15, gate.set).start()
    inst.start()
    assert inst._pool is not None, "start() returned before the pool was ready"
    inst.close()


def test_start_raises_clear_error_when_pool_init_fails(monkeypatch):
    """A genuinely unreachable DB must fail fast with a clear RuntimeError, not
    leave callers to hit a None pool."""
    async def failing_create_pool(self):
        raise OSError("connection refused")

    monkeypatch.setattr(PipelineDB, "_create_pool", failing_create_pool)
    monkeypatch.setattr(dbmod, "POOL_READY_TIMEOUT", 5)

    inst = PipelineDB()
    with pytest.raises(RuntimeError):
        inst.start()
    inst.close()
