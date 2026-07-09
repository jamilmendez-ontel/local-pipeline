# swift_api_pipeline/tests/test_db_tx.py
"""Live smoke test for db_tx.py, the transaction-pooler DB module.

Needs swift_api_pipeline/.env (SUPABASE_PASSWORD at minimum) and network
reachability to the Supabase pooler host. Unlike db.py's direct-host tests,
this does NOT require Cloudflare WARP: the transaction pooler resolves over
IPv4 (aws-0-ap-southeast-1.pooler.supabase.com), the direct host does not.

Proves:
  1. A plain query works (SELECT 1).
  2. Parameterized statements work repeatedly without relying on named
     prepared statements (PgBouncer transaction mode cannot hold those
     across transactions; statement_cache_size=0 must be in effect).
  3. The pool opens with zero pre-warmed connections (min_size=0): no idle
     server slots held on the pooler.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from db_tx import get_tx_db, close_tx_db


def test_tx_pool_opens_with_zero_idle_connections():
    db = get_tx_db()
    try:
        # min_size=0: the pool must not pre-open any connections.
        assert db._pool.get_size() == 0
        assert db._pool.get_idle_size() == 0
    finally:
        close_tx_db()


def test_tx_roundtrip():
    db = get_tx_db()
    try:
        assert db.fetchval("SELECT 1") == 1
        # Two parameterized calls in a row must survive PgBouncer transaction
        # mode's lack of named-prepared-statement persistence.
        assert db.fetchval("SELECT $1::int + 1", 41) == 42
        assert db.fetchval("SELECT $1::int + 1", 1) == 2
    finally:
        close_tx_db()
