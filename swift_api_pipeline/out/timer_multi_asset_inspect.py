"""
Export every timer site whose connecting key maps to MULTIPLE assets, flattened
to one row per candidate asset, so we can verify the ambiguity by eye.

Two sheets:
  by_path : timer site_id (folder path) maps to >1 distinct asset (same project)
  by_name : timer site_name maps to >1 distinct asset (same project)

Grouping unit is the DISTINCT timer site (project_did, site_id, site_name) -- every
timer punch on the same site has the identical candidate set, so we collapse the
punches and carry a timer_rows count instead of repeating them.

Output: out/timer_multi_asset_inspect.xlsx
Run:    venv\\Scripts\\python.exe out\\timer_multi_asset_inspect.py
"""
import asyncio
import os
from pathlib import Path

import asyncpg
import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")

PROJECT_REF = "voqfjfngdpcvevbkikud"
DSN = dict(
    host="aws-0-ap-southeast-1.pooler.supabase.com",
    port=6543,
    database=os.environ.get("SUPABASE_DB", "postgres"),
    user=f"postgres.{PROJECT_REF}",
    password=os.environ["SUPABASE_PASSWORD"],
    statement_cache_size=0,
    ssl="require",
)

# key_col: the asset column the timer key is compared against ('asset_id' or 'asset_name')
# timer_col: the timer column that holds the key ('site_id' or 'site_name')
QUERY = """
WITH amb AS (
    SELECT project_did, {key_col} AS key
    FROM data_staging.stg_assets
    WHERE {key_col} IS NOT NULL AND asset_did IS NOT NULL
    GROUP BY project_did, {key_col}
    HAVING COUNT(DISTINCT asset_did) > 1
),
timer_sites AS (
    SELECT t.project_did, t.project, t.site_id, t.site_name,
           COUNT(*)                  AS timer_rows,
           COUNT(DISTINCT t.asset_did) AS distinct_assigned_dids,
           MIN(t.asset_did)          AS assigned_asset_did
    FROM data_staging.stg_timer_activities t
    JOIN amb a ON a.project_did = t.project_did AND a.key = t.{timer_col}
    GROUP BY t.project_did, t.project, t.site_id, t.site_name
),
candidates AS (
    SELECT DISTINCT project_did, {key_col} AS key, asset_did, asset_name, asset_id, asset_status
    FROM data_staging.stg_assets
    WHERE {key_col} IS NOT NULL AND asset_did IS NOT NULL
)
SELECT
    ts.project,
    ts.project_did,
    ts.site_name                                  AS timer_site_name,
    ts.site_id                                    AS timer_path,
    ts.timer_rows,
    ts.distinct_assigned_dids,
    ts.assigned_asset_did,
    c.asset_name                                  AS candidate_asset_name,
    c.asset_did                                   AS candidate_asset_did,
    c.asset_id                                    AS candidate_asset_path,
    c.asset_status                                AS candidate_asset_status,
    (c.asset_did  = ts.assigned_asset_did)        AS is_currently_assigned,
    (c.asset_name = ts.site_name)                 AS name_matches_timer
FROM timer_sites ts
JOIN candidates c
  ON c.project_did = ts.project_did AND c.key = ts.{timer_col}
ORDER BY ts.project, ts.{timer_col}, ts.site_name, c.asset_name;
"""

PATH_COLS = [
    "group_no", "project", "timer_site_name",
    "candidate_asset_name", "name_matches_timer", "is_currently_assigned",
    "candidate_asset_did", "assigned_asset_did", "candidate_asset_status",
    "timer_rows", "distinct_assigned_dids",
    "timer_path", "candidate_asset_path", "project_did",
]


async def fetch(conn, key_col, timer_col):
    q = QUERY.format(key_col=key_col, timer_col=timer_col)
    rows = await conn.fetch(q)
    return pd.DataFrame([dict(r) for r in rows])


def add_group_no(df, group_keys):
    if df.empty:
        df["group_no"] = []
        return df
    df = df.copy()
    df["group_no"] = df.groupby(group_keys, sort=False).ngroup() + 1
    return df


async def main():
    conn = await asyncpg.connect(**DSN)
    try:
        df_path = await fetch(conn, "asset_id", "site_id")
        df_name = await fetch(conn, "asset_name", "site_name")
    finally:
        await conn.close()

    df_path = add_group_no(df_path, ["project_did", "timer_path", "timer_site_name"])
    df_name = add_group_no(df_name, ["project_did", "timer_site_name"])

    df_path = df_path[PATH_COLS]
    df_name = df_name[PATH_COLS]

    out = HERE / "timer_multi_asset_inspect.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        df_path.to_excel(xl, sheet_name="by_path", index=False)
        df_name.to_excel(xl, sheet_name="by_name", index=False)
        for name in ("by_path", "by_name"):
            ws = xl.sheets[name]
            ws.freeze_panes = "A2"
            widths = {"A": 9, "B": 20, "C": 34, "D": 34, "E": 16, "F": 18,
                      "G": 24, "H": 24, "I": 16, "J": 11, "K": 14,
                      "L": 50, "M": 50, "N": 22}
            for col, w in widths.items():
                ws.column_dimensions[col].width = w

    def summarize(df, label):
        if df.empty:
            print(f"{label}: 0 rows")
            return
        groups = df["group_no"].nunique()
        candidates = len(df)
        suspect = int((df["is_currently_assigned"] & ~df["name_matches_timer"]).sum())
        print(f"{label}: {groups} ambiguous timer sites, {candidates} candidate-asset rows")
        print(f"    candidate rows that are the ASSIGNED one but name does NOT match: {suspect}")

    print(f"Wrote -> {out}")
    summarize(df_path, "by_path")
    summarize(df_name, "by_name")


if __name__ == "__main__":
    asyncio.run(main())
