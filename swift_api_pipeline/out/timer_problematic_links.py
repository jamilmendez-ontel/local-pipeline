"""
Export ONLY the problematic timer->asset links: rows whose currently-assigned
asset has a name that does not match what the technician typed (whitespace-
normalized), or whose assigned did points to no asset at all.

Clean / correct links are deliberately excluded.

For each problem site it also shows the recommended action:
  - RE-POINT  : an exact-name asset exists in the same project (the right target)
  - NULL      : no exact-name asset exists (e.g. '(Civil)'), or the name is itself
                ambiguous (maps to multiple assets), or the did is an orphan.

Grain: distinct timer site (project_did, site_id, site_name, assigned asset_did),
with a timer_rows count. Source = stg_timer_activities_clean (what users see).

Output: out/timer_problematic_links.xlsx
Run:    venv\\Scripts\\python.exe out\\timer_problematic_links.py
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

QUERY = """
WITH asset_lookup AS (
    SELECT asset_did, MIN(asset_name) AS asset_name, MIN(asset_id) AS asset_path
    FROM data_staging.stg_assets
    WHERE asset_did IS NOT NULL
    GROUP BY asset_did
),
sites AS (
    SELECT c.project_did, c.project, c.site_id, c.site_name, c.asset_did,
           COUNT(*) AS timer_rows
    FROM data_staging.stg_timer_activities_clean c
    WHERE c.asset_did IS NOT NULL
    GROUP BY c.project_did, c.project, c.site_id, c.site_name, c.asset_did
),
problem AS (
    SELECT s.*, a.asset_name AS assigned_name, a.asset_path AS assigned_path
    FROM sites s
    LEFT JOIN asset_lookup a ON a.asset_did = s.asset_did
    WHERE a.asset_name IS NULL
       OR TRIM(s.site_name) <> TRIM(a.asset_name)
)
SELECT
    p.project,
    p.site_name              AS timer_site_name,
    p.assigned_name          AS assigned_asset_name,
    CASE
        WHEN p.assigned_name IS NULL          THEN 'NULL  (assigned did has no asset = orphan)'
        WHEN g.n_dids = 1 AND g.in_project    THEN 'RE-POINT (exact-name asset in same project)'
        WHEN g.n_dids = 1                     THEN 'RE-POINT (exact-name asset in another project)'
        WHEN g.n_dids > 1                     THEN 'NULL  (name maps to multiple assets)'
        ELSE                                       'NULL  (no asset has this exact name anywhere)'
    END                      AS recommended_action,
    g.correct_name           AS should_be_asset_name,
    g.correct_did            AS should_be_asset_did,
    g.in_project             AS target_in_same_project,
    p.asset_did              AS currently_assigned_did,
    g.n_dids                 AS exact_name_assets_total,
    p.timer_rows,
    p.site_id                AS timer_path,
    p.assigned_path          AS assigned_asset_path,
    p.project_did
FROM problem p
LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT a.asset_did)            AS n_dids,
           MIN(a.asset_did)                       AS correct_did,
           MIN(a.asset_name)                      AS correct_name,
           bool_or(a.project_did = p.project_did) AS in_project
    FROM data_staging.stg_assets a
    WHERE TRIM(a.asset_name) = TRIM(p.site_name)
      AND a.asset_did IS NOT NULL
) g ON TRUE
ORDER BY recommended_action, p.project, p.site_name;
"""

COLS = [
    "recommended_action",
    "project", "timer_site_name",
    "assigned_asset_name", "should_be_asset_name",
    "target_in_same_project",
    "currently_assigned_did", "should_be_asset_did",
    "exact_name_assets_total", "timer_rows",
    "timer_path", "assigned_asset_path", "project_did",
]


async def main():
    conn = await asyncpg.connect(**DSN)
    try:
        rows = await conn.fetch(QUERY)
    finally:
        await conn.close()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        print("No problematic links found.")
        return
    df = df[COLS]

    out = HERE / "timer_problematic_links.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="problematic", index=False)
        ws = xl.sheets["problematic"]
        ws.freeze_panes = "A2"
        widths = {"A": 46, "B": 20, "C": 36, "D": 36, "E": 36,
                  "F": 20, "G": 24, "H": 24, "I": 14, "J": 11,
                  "K": 50, "L": 50, "M": 22}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w

    print(f"Wrote {len(df)} problematic timer sites -> {out}")
    print(f"  total affected timer rows: {int(df['timer_rows'].sum())}")
    for action, grp in df.groupby("recommended_action"):
        print(f"  {len(grp):>4} sites / {int(grp['timer_rows'].sum()):>5} rows  {action}")


if __name__ == "__main__":
    asyncio.run(main())
