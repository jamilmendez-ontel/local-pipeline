"""Apply migration 024: Increase RPC statement timeouts to 600s."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from config import get_db

def main():
    db = get_db()
    sql_file = Path(__file__).with_name("024_increase_rpc_timeouts.sql")
    sql = sql_file.read_text(encoding="utf-8")

    # Split on semicolons, skip empty statements
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]

    # The CREATE OR REPLACE FUNCTION contains semicolons inside the body,
    # so we need to handle dollar-quoting. Split on $$ boundaries instead.
    # Strategy: find the ALTER FUNCTION (first statement before $$), then
    # the CREATE OR REPLACE FUNCTION (everything between first $$ and last $$).

    # Simpler: execute the whole file as one statement won't work in asyncpg.
    # Instead, split manually:
    # Statement 1: ALTER FUNCTION ... SET statement_timeout = '600s'
    # Statement 2: CREATE OR REPLACE FUNCTION ... $$ ... $$;

    idx = sql.find("CREATE OR REPLACE FUNCTION")
    part1 = sql[:idx]  # ALTER FUNCTION line(s)
    part2 = sql[idx:]   # CREATE OR REPLACE FUNCTION block

    # Execute ALTER FUNCTION statements
    for line in part1.strip().split("\n"):
        line = line.strip()
        if line and not line.startswith("--"):
            # Remove trailing semicolons for asyncpg
            line = line.rstrip(";")
            if line:
                print(f"Executing: {line[:80]}...")
                db.execute(line, statement_timeout=30)

    # Execute the CREATE OR REPLACE FUNCTION as one block
    # Remove trailing whitespace/semicolons outside the $$ block
    part2 = part2.strip()
    if part2.endswith(";"):
        part2 = part2[:-1].strip()
    print(f"Executing: CREATE OR REPLACE FUNCTION data_staging.backfill_asset_did()...")
    db.execute(part2, statement_timeout=30)

    print("Migration 024 applied successfully!")


if __name__ == "__main__":
    main()
