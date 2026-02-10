#!/usr/bin/env python3
"""Apply migration 014: aggregate_assets_from_raw RPC function"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()


async def main():
    import asyncpg

    dsn = os.getenv("SUPABASE_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

    conn = await asyncpg.connect(dsn)
    try:
        sql_path = os.path.join(os.path.dirname(__file__), "014_aggregate_assets_rpc.sql")
        with open(sql_path) as f:
            sql = f.read()

        await conn.execute(sql)
        print("Migration 014 applied successfully: aggregate_assets_from_raw RPC created")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
