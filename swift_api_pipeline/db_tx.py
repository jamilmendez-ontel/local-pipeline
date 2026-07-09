"""
db_tx.py -- asyncpg connection pool for the Supabase TRANSACTION-mode pooler
(port 6543), with the same synchronous bridge shape as db.py.

This is a SEPARATE pool from db.py's session-mode pool: it never imports or
mutates db.py's module state, and callers who need both open both.

Why a second module instead of a flag on db.py:
  - The transaction pooler and the session pooler are reached at different
    hostnames with different credentials (see below), so the two pools are
    never interchangeable at connect time.
  - The connection-handling rules below are specific to PgBouncer transaction
    mode and would be easy to violate by accident if mixed into db.py's
    session-mode code path.

Differences from db.py, and why:

  - statement_cache_size=0: PgBouncer in transaction mode hands out a
    different backend server connection per transaction, so a named
    prepared statement created on one backend will not exist on the next.
    asyncpg's default behavior of auto-preparing statements and caching
    them by name would break on the second use with "prepared statement
    does not exist". Setting statement_cache_size=0 makes asyncpg send
    every statement unnamed (extended-query "unnamed" prepare each time),
    which the transaction pooler tolerates.

  - min_size=0: the pool must hold NO idle server slots between calls.
    Transaction-mode pooling exists specifically to multiplex many client
    pools over a small number of Postgres backends; pre-warming connections
    like db.py's session pool does would defeat that and re-create the
    "all clients capped" pile-up db.py had to work around.

  - No session-level SET, anywhere in this module: db.py re-applies
    "SET statement_timeout" on every acquire because asyncpg's pool runs
    RESET ALL when a connection is returned to IT, but that guarantee is
    about asyncpg's own pool, not PgBouncer. In transaction mode, PgBouncer
    itself may swap the underlying Postgres backend between transactions on
    the SAME client socket, and a plain "SET" sent outside an explicit
    transaction is not guaranteed to be undone or to stick around for the
    next statement. Callers who need a non-default setting (e.g. a longer
    statement_timeout) must wrap their own explicit transaction and issue
    "SET LOCAL ..." inside it: SET LOCAL is scoped to the current
    transaction and is always rolled back/discarded when it ends, which is
    the only safe pattern under transaction-mode pooling.

  - Its own host/user env vars (SUPABASE_TX_HOST / SUPABASE_TX_USER), not
    db.py's SUPABASE_HOST / SUPABASE_USER: the direct host
    (db.voqfjfngdpcvevbkikud.supabase.co, from SUPABASE_HOST) is IPv6-only
    and has no pooler listening on it at all, only Postgres on 5432. The
    transaction pooler only exists at the separate pooler hostname
    (aws-0-ap-southeast-1.pooler.supabase.com), which also resolves over
    IPv4, so this module works without Cloudflare WARP. The pooler also
    requires the project-suffixed user ("postgres.<project-ref>") to route
    the connection to the right project, whereas the direct host's plain
    "postgres" user (SUPABASE_USER, often just "postgres" locally) would
    not be accepted there. SUPABASE_DB and SUPABASE_PASSWORD are unchanged
    and reused from db.py's env.

Architecture (same as db.py):
  - One asyncio event loop runs in a daemon thread
  - asyncpg pool lives on that loop
  - Sync callers (ThreadPoolExecutor workers) use thin wrappers via run_coroutine_threadsafe()
  - Thread-safe by design: asyncpg pool handles concurrency internally
"""

import os
import json
import ssl
import time
import logging
import asyncio
import threading
from typing import Any, List, Optional, Sequence

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Use standard logging directly to avoid circular import with config.py
logger = logging.getLogger("pipeline.db_tx")

# Build DSN from env vars. Host/port/user are TX-specific (pooler-only);
# database/password are shared with db.py's env (SUPABASE_DB / SUPABASE_PASSWORD).
_TX_HOST = os.getenv("SUPABASE_TX_HOST", "aws-0-ap-southeast-1.pooler.supabase.com")
_TX_PORT = os.getenv("SUPABASE_TX_PORT", "6543")
_DB = os.getenv("SUPABASE_DB", "postgres")
_TX_USER = os.getenv("SUPABASE_TX_USER", "postgres.voqfjfngdpcvevbkikud")
_PASS = os.getenv("SUPABASE_PASSWORD", "")
TX_DSN = f"postgresql://{_TX_USER}:{_PASS}@{_TX_HOST}:{_TX_PORT}/{_DB}"

# Pool-init tuning, mirrors db.py's rationale: cap each connect attempt so a
# slow one fails fast and the retry loop runs, and wait for the FULL retry
# budget before declaring failure.
POOL_CONNECT_TIMEOUT = 15   # seconds per asyncpg connection attempt
POOL_MAX_ATTEMPTS = 4       # _create_pool retries
POOL_BASE_DELAY = 3         # seconds, doubles each retry: 3, 6, 12
POOL_READY_TIMEOUT = 90     # seconds start() waits; must exceed total retry budget (~81s)

# min_size=0: hold NO idle server slots on the pooler between calls (see
# module docstring). The pool still grows on demand up to max_size.
POOL_MIN_SIZE = 0
POOL_MAX_SIZE = int(os.getenv("TX_POOL_MAX_SIZE", "10"))


def _jsonb_binary_encoder(value):
    """Encode Python object to PostgreSQL JSONB binary format (version byte + UTF-8 JSON)."""
    return b'\x01' + json.dumps(value).encode('utf-8')


def _jsonb_binary_decoder(data):
    """Decode PostgreSQL JSONB binary format to Python object."""
    # Strip the version byte (0x01)
    return json.loads(data[1:])


async def _init_connection(conn: asyncpg.Connection):
    """Called for every new physical connection in the pool. Registers
    JSONB/JSON codecs only, client-side, no SQL round-trip: NOT the place
    for session-level SET (see module docstring)."""
    # Text protocol codecs (for execute/fetch/executemany)
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    # Binary protocol codecs (for COPY)
    await conn.set_type_codec(
        "jsonb",
        encoder=_jsonb_binary_encoder,
        decoder=_jsonb_binary_decoder,
        schema="pg_catalog",
        format="binary",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda v: json.dumps(v).encode('utf-8'),
        decoder=lambda d: json.loads(d),
        schema="pg_catalog",
        format="binary",
    )


class TxDB:
    """Manages an asyncpg pool against the transaction-mode pooler, on a
    background event loop, with a sync API. See module docstring for why
    this differs from db.py's PipelineDB."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background loop thread and block until the pool is ready.

        ALWAYS waits for readiness, even when a thread is already alive: a
        prior start() may have raised its timeout while the background
        thread was still retrying _create_pool (pool still None)."""
        if self._thread is None or not self._thread.is_alive():
            self._ready = threading.Event()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="db-tx-loop")
            self._thread.start()
        if not self._ready.wait(timeout=POOL_READY_TIMEOUT):
            raise RuntimeError(f"Transaction-pooler DB pool failed to initialize within {POOL_READY_TIMEOUT}s")
        if self._pool is None:
            raise RuntimeError("Transaction-pooler DB pool thread exited without creating a pool")
        logger.info(f"Transaction-pooler DB pool ready (min={POOL_MIN_SIZE}, max={POOL_MAX_SIZE})")

    def _run_loop(self):
        """Entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._create_pool())
        except Exception:
            # All connect attempts failed. Signal start() (which checks _pool is
            # None and raises) instead of leaving it blocked for the full timeout.
            logger.exception("Transaction-pooler DB pool initialization failed in background loop")
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _create_pool(self):
        """Create the asyncpg connection pool with retry on transient failures."""
        # Supabase's pooler also requires SSL
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        max_attempts = POOL_MAX_ATTEMPTS
        base_delay = POOL_BASE_DELAY  # seconds, doubles each retry

        for attempt in range(1, max_attempts + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    TX_DSN,
                    min_size=POOL_MIN_SIZE,
                    max_size=POOL_MAX_SIZE,
                    command_timeout=300,
                    timeout=POOL_CONNECT_TIMEOUT,  # per-connection acquire cap; slow connects fail fast and retry
                    init=_init_connection,
                    ssl=ssl_ctx,
                    statement_cache_size=0,
                )
                return
            except Exception as e:
                if attempt == max_attempts:
                    logger.error(
                        f"Failed to create transaction-pooler pool after {max_attempts} attempts: "
                        f"{type(e).__name__}: {e}"
                    )
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Transaction-pooler pool creation failed (attempt {attempt}/{max_attempts}): "
                    f"{type(e).__name__}: {e}. Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)

    def close(self):
        """Shut down pool and event loop."""
        if self._pool and self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._pool.close(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            logger.info("Transaction-pooler DB pool closed")

    def reconnect(self):
        """Close stale pool and create a fresh one. Blocks until ready."""
        logger.warning("Reconnecting transaction-pooler DB pool...")
        if self._pool and self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._pool.close(), self._loop)
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)

        self._pool = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self.start()
        logger.info("Transaction-pooler DB pool reconnected successfully")

    # ------------------------------------------------------------------
    # Internal bridge: submit coroutine, block for result
    # ------------------------------------------------------------------

    def _run(self, coro):
        """Submit a coroutine to the event loop and block until done."""
        if self._pool is None or not self._loop or not self._loop.is_running():
            coro.close()  # avoid "coroutine was never awaited" warning
            raise RuntimeError("Transaction-pooler DB pool is not initialized. Call start() first.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ------------------------------------------------------------------
    # Public sync API (thread-safe)
    # ------------------------------------------------------------------
    #
    # No SET of any kind is issued here (unlike db.py's per-acquire
    # statement_timeout SET): see module docstring. Callers needing
    # non-default settings must open their own explicit transaction and
    # issue SET LOCAL inside it.

    def execute(self, query: str, *args, timeout: float = None) -> str:
        """Execute a query and return the status string."""
        async def _do():
            async with self._pool.acquire() as conn:
                return await conn.execute(query, *args, timeout=timeout)
        return self._run(_do())

    def fetch(self, query: str, *args, timeout: float = None) -> List[asyncpg.Record]:
        """Execute a query and return all rows."""
        async def _do():
            async with self._pool.acquire() as conn:
                return await conn.fetch(query, *args, timeout=timeout)
        return self._run(_do())

    def fetchrow(self, query: str, *args, timeout: float = None) -> Optional[asyncpg.Record]:
        """Execute a query and return the first row."""
        async def _do():
            async with self._pool.acquire() as conn:
                return await conn.fetchrow(query, *args, timeout=timeout)
        return self._run(_do())

    def fetchval(self, query: str, *args, column: int = 0, timeout: float = None) -> Any:
        """Execute a query and return a single value."""
        async def _do():
            async with self._pool.acquire() as conn:
                return await conn.fetchval(query, *args, column=column, timeout=timeout)
        return self._run(_do())

    def executemany(self, query: str, args: Sequence[Sequence], timeout: float = None) -> None:
        """Execute a query for each set of args."""
        async def _do():
            async with self._pool.acquire() as conn:
                return await conn.executemany(query, args, timeout=timeout)
        return self._run(_do())


# ------------------------------------------------------------------
# Singleton + retry helper
# ------------------------------------------------------------------

_tx_db_instance: Optional[TxDB] = None
_tx_db_lock = threading.Lock()


def get_tx_db() -> TxDB:
    """Get or create the singleton TxDB instance. Independent of db.py's
    singleton: never touches db.py's module state."""
    global _tx_db_instance
    if _tx_db_instance is None:
        with _tx_db_lock:
            if _tx_db_instance is None:
                _tx_db_instance = TxDB()
                _tx_db_instance.start()
    return _tx_db_instance


def close_tx_db():
    """Close the singleton TxDB instance."""
    global _tx_db_instance
    if _tx_db_instance is not None:
        _tx_db_instance.close()
        _tx_db_instance = None


def reconnect_tx_db():
    """Reconnect the singleton TxDB instance (fresh pool)."""
    global _tx_db_instance
    with _tx_db_lock:
        if _tx_db_instance is not None:
            _tx_db_instance.reconnect()
        else:
            _tx_db_instance = TxDB()
            _tx_db_instance.start()
    return _tx_db_instance


# Connection-level exceptions that warrant a pool reconnect
_CONNECTION_ERRORS = (
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.InterfaceError,
    OSError,
)


def retry_tx_db(fn, max_retries=5, description="operation"):
    """Execute a database operation with retry, exponential backoff,
    and automatic pool reconnect on connection-level failures."""
    _logger = logging.getLogger("pipeline.retry_tx")
    for attempt in range(max_retries):
        try:
            return fn()
        except _CONNECTION_ERRORS as e:
            if attempt == max_retries - 1:
                raise
            wait = min(2 ** attempt, 15)
            _logger.warning(
                f"{description} connection lost (attempt {attempt + 1}/{max_retries}): "
                f"{type(e).__name__}: {e}. Reconnecting pool in {wait}s..."
            )
            time.sleep(wait)
            reconnect_tx_db()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = min(2 ** attempt, 15)
            _logger.warning(f"{description} failed (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {e}. Retrying in {wait}s...")
            time.sleep(wait)
