"""
db.py -- asyncpg connection pool with synchronous bridge for pipeline threads.

Architecture:
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
from typing import Any, List, Optional, Sequence, Tuple

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# Use standard logging directly to avoid circular import with config.py
logger = logging.getLogger("pipeline.db")

# Build DSN from env vars
_HOST = os.getenv("SUPABASE_HOST", "db.voqfjfngdpcvevbkikud.supabase.co")
_PORT = os.getenv("SUPABASE_PORT", "5432")
_DB = os.getenv("SUPABASE_DB", "postgres")
_USER = os.getenv("SUPABASE_USER", "postgres")
_PASS = os.getenv("SUPABASE_PASSWORD", "")
DSN = f"postgresql://{_USER}:{_PASS}@{_HOST}:{_PORT}/{_DB}"


def _jsonb_binary_encoder(value):
    """Encode Python object to PostgreSQL JSONB binary format (version byte + UTF-8 JSON)."""
    return b'\x01' + json.dumps(value).encode('utf-8')


def _jsonb_binary_decoder(data):
    """Decode PostgreSQL JSONB binary format to Python object."""
    # Strip the version byte (0x01)
    return json.loads(data[1:])


async def _init_connection(conn: asyncpg.Connection):
    """Called for every new connection in the pool. Register JSONB/JSON codecs."""
    # Set server-side statement timeout (Supabase default is ~8s, we need more)
    await conn.execute("SET statement_timeout = '300s'")
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


class PipelineDB:
    """Manages an asyncpg pool on a background event loop with sync API."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background event loop thread and create the pool."""
        if self._thread is not None and self._thread.is_alive():
            return  # Already running

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="db-loop")
        self._thread.start()
        # Wait for pool to be ready (up to 30s)
        if not self._ready.wait(timeout=30):
            raise RuntimeError("Database pool failed to initialize within 30s")
        logger.info(f"Database pool ready (min=4, max=20)")

    def _run_loop(self):
        """Entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._create_pool())
        self._ready.set()
        self._loop.run_forever()

    async def _create_pool(self):
        """Create the asyncpg connection pool."""
        # Supabase cloud requires SSL
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        self._pool = await asyncpg.create_pool(
            DSN,
            min_size=4,
            max_size=20,
            command_timeout=300,
            init=_init_connection,
            ssl=ssl_ctx,
        )

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
            logger.info("Database pool closed")

    # ------------------------------------------------------------------
    # Internal bridge: submit coroutine, block for result
    # ------------------------------------------------------------------

    def _run(self, coro):
        """Submit a coroutine to the event loop and block until done."""
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Database event loop is not running. Call start() first.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # ------------------------------------------------------------------
    # Public sync API (thread-safe)
    # ------------------------------------------------------------------

    # Note: asyncpg pool runs RESET ALL when connections are returned,
    # which clears session-level SET commands. We must re-apply
    # statement_timeout on every acquire, not just in _init_connection.
    _TIMEOUT_SQL = "SET statement_timeout = '300s'"

    def execute(self, query: str, *args, timeout: float = None, statement_timeout: int = None) -> str:
        """Execute a query and return the status string.

        Args:
            timeout: Client-side timeout in seconds (overrides pool command_timeout=300).
            statement_timeout: Override server-side statement_timeout in seconds
                              (default: 300s from _TIMEOUT_SQL). Also sets client-side
                              timeout to match if timeout is not explicitly provided.
        """
        async def _do():
            async with self._pool.acquire() as conn:
                if statement_timeout is not None:
                    await conn.execute(f"SET statement_timeout = '{statement_timeout}s'")
                else:
                    await conn.execute(self._TIMEOUT_SQL)
                # Use statement_timeout as client timeout too, unless explicitly overridden
                effective_timeout = timeout if timeout is not None else (statement_timeout or None)
                return await conn.execute(query, *args, timeout=effective_timeout)
        return self._run(_do())

    def fetch(self, query: str, *args, timeout: float = None, statement_timeout: int = None) -> List[asyncpg.Record]:
        """Execute a query and return all rows.

        Args:
            timeout: Client-side timeout in seconds.
            statement_timeout: Override server-side statement_timeout in seconds.
                              Also sets client-side timeout to match if timeout
                              is not explicitly provided.
        """
        async def _do():
            async with self._pool.acquire() as conn:
                if statement_timeout is not None:
                    await conn.execute(f"SET statement_timeout = '{statement_timeout}s'")
                else:
                    await conn.execute(self._TIMEOUT_SQL)
                effective_timeout = timeout if timeout is not None else (statement_timeout or None)
                return await conn.fetch(query, *args, timeout=effective_timeout)
        return self._run(_do())

    def fetchrow(self, query: str, *args, timeout: float = None, statement_timeout: int = None) -> Optional[asyncpg.Record]:
        """Execute a query and return the first row."""
        async def _do():
            async with self._pool.acquire() as conn:
                if statement_timeout is not None:
                    await conn.execute(f"SET statement_timeout = '{statement_timeout}s'")
                else:
                    await conn.execute(self._TIMEOUT_SQL)
                effective_timeout = timeout if timeout is not None else (statement_timeout or None)
                return await conn.fetchrow(query, *args, timeout=effective_timeout)
        return self._run(_do())

    def fetchval(self, query: str, *args, column: int = 0, timeout: float = None, statement_timeout: int = None) -> Any:
        """Execute a query and return a single value."""
        async def _do():
            async with self._pool.acquire() as conn:
                if statement_timeout is not None:
                    await conn.execute(f"SET statement_timeout = '{statement_timeout}s'")
                else:
                    await conn.execute(self._TIMEOUT_SQL)
                effective_timeout = timeout if timeout is not None else (statement_timeout or None)
                return await conn.fetchval(query, *args, column=column, timeout=effective_timeout)
        return self._run(_do())

    def executemany(self, query: str, args: Sequence[Sequence], timeout: float = None) -> None:
        """Execute a query for each set of args."""
        async def _do():
            async with self._pool.acquire() as conn:
                await conn.execute(self._TIMEOUT_SQL)
                return await conn.executemany(query, args, timeout=timeout)
        return self._run(_do())

    def copy_records(
        self,
        table: str,
        *,
        schema_name: str,
        records: List[Tuple],
        columns: List[str],
        timeout: float = 600,
    ) -> str:
        """COPY records into a table using asyncpg's binary COPY protocol.

        Uses a 600s timeout (both client and server-side) since large JSONB
        batches under concurrent load can exceed the default 300s command_timeout.
        """
        async def _do():
            async with self._pool.acquire() as conn:
                await conn.execute(f"SET statement_timeout = '{int(timeout)}s'")
                return await conn.copy_records_to_table(
                    table,
                    schema_name=schema_name,
                    records=records,
                    columns=columns,
                    timeout=timeout,
                )
        return self._run(_do())


# ------------------------------------------------------------------
# Singleton + retry helper
# ------------------------------------------------------------------

_db_instance: Optional[PipelineDB] = None
_db_lock = threading.Lock()


def get_db() -> PipelineDB:
    """Get or create the singleton PipelineDB instance."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = PipelineDB()
                _db_instance.start()
    return _db_instance


def close_db():
    """Close the singleton PipelineDB instance."""
    global _db_instance
    if _db_instance is not None:
        _db_instance.close()
        _db_instance = None


def retry_db(fn, max_retries=5, description="operation"):
    """Execute a database operation with retry and exponential backoff."""
    _logger = logging.getLogger("pipeline.retry")
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = min(2 ** attempt, 15)
            _logger.warning(f"{description} failed (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {e}. Retrying in {wait}s...")
            time.sleep(wait)
