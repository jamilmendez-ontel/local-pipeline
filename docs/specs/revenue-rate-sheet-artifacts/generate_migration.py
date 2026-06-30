"""
One-shot generator: reads the Revenue Metrics xlsx and emits a SQL migration
that creates reference.ref_task_revenue_rates and inserts all rows.

Run:
    python generate_migration.py

Outputs migration_revenue_rates.sql next to this script.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import openpyxl

SOURCE_XLSX = Path(r"C:\Users\admin\Downloads\Revenue Metrics_Duration & Amount (1).xlsx")
OUTPUT_SQL = Path(__file__).parent / "migration_revenue_rates.sql"
SOURCE_FILE_LABEL = SOURCE_XLSX.name

PREFIX_RE = re.compile(r"^\d+\.\s+")


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    wb = openpyxl.load_workbook(SOURCE_XLSX, data_only=True)
    ws = wb["Sheet1"]

    rows: list[tuple[str, str, str, Decimal, Decimal]] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            # Header row: Market/Project, Task, Duration Hrs, Amount
            continue
        market, task, dur, amt = row[0], row[1], row[2], row[3]
        if market is None or task is None:
            continue
        market = str(market).strip()
        task = str(task).strip()
        task_norm = PREFIX_RE.sub("", task)
        dur = Decimal(str(dur if dur is not None else 0))
        amt = Decimal(str(amt if amt is not None else 0))
        rows.append((market, task, task_norm, dur, amt))

    lines: list[str] = []
    lines.append("-- Migration: add_ref_task_revenue_rates")
    lines.append("-- Source: " + SOURCE_FILE_LABEL)
    lines.append("-- Spec: docs/specs/revenue-rate-sheet.md")
    lines.append("")
    lines.append("CREATE TABLE IF NOT EXISTS reference.ref_task_revenue_rates (")
    lines.append("  id              bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,")
    lines.append("  market_bucket   text NOT NULL,")
    lines.append("  task_name       text NOT NULL,")
    lines.append("  task_name_norm  text NOT NULL,")
    lines.append("  duration_hrs    numeric(5,2) NOT NULL,")
    lines.append("  amount_usd      numeric(10,2) NOT NULL,")
    lines.append("  source_file     text,")
    lines.append("  created_at      timestamptz DEFAULT now(),")
    lines.append("  updated_at      timestamptz DEFAULT now(),")
    lines.append("  UNIQUE (market_bucket, task_name)")
    lines.append(");")
    lines.append("")
    lines.append("CREATE INDEX IF NOT EXISTS idx_revenue_rates_market")
    lines.append("  ON reference.ref_task_revenue_rates(market_bucket);")
    lines.append("CREATE INDEX IF NOT EXISTS idx_revenue_rates_task_norm")
    lines.append("  ON reference.ref_task_revenue_rates(task_name_norm);")
    lines.append("")
    lines.append(
        "INSERT INTO reference.ref_task_revenue_rates "
        "(market_bucket, task_name, task_name_norm, duration_hrs, amount_usd, source_file) VALUES"
    )

    value_lines = []
    for market, task, task_norm, dur, amt in rows:
        value_lines.append(
            f"  ({sql_str(market)}, {sql_str(task)}, {sql_str(task_norm)}, "
            f"{dur}, {amt}, {sql_str(SOURCE_FILE_LABEL)})"
        )
    lines.append(",\n".join(value_lines) + ";")
    lines.append("")

    OUTPUT_SQL.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(rows)} rows to {OUTPUT_SQL}")


if __name__ == "__main__":
    main()
